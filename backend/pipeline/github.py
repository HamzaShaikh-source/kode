"""GitHub pipeline (PyGithub): branch, multi-file blob commit under the mapped human identity, PR,
and merge. merge_pr() is the ONLY merge path and refuses anything that is not an APPROVED request."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Dict, Protocol

from github import Github, GithubException, InputGitAuthor, InputGitTreeElement

from config import MissingConfigError, Settings
from models import ApprovalRequest, ApprovalStatus, CommitRef, PRRef


class MergeNotApproved(PermissionError):
    """Attempted to merge without an approved ApprovalRequest."""


class GitHubPort(Protocol):
    async def create_branch(self, name: str) -> str: ...
    async def commit_files(self, branch: str, files: Dict[str, str], message: str,
                           author_name: str, author_email: str) -> CommitRef: ...
    async def open_pr(self, branch: str, title: str, body: str) -> PRRef: ...
    async def comment(self, pr_number: int, body: str) -> None: ...
    async def merge_pr(self, approval: ApprovalRequest) -> str: ...
    async def close_pr(self, pr_number: int, reason: str) -> None: ...
    async def head_sha(self, ref: str) -> str: ...


class GitHubClient:
    """Real GitHub via PyGithub. Blocking calls run in a worker thread so agents stay concurrent."""

    def __init__(self, settings: Settings, gh=None):
        settings.require_github()
        if settings.simulated_github:
            raise MissingConfigError("SIMULATED_GITHUB=true but no simulator exists; refusing to fake GitHub.")
        self.base = settings.github_base_branch
        self.gh = gh or Github(settings.github_token.get_secret_value())
        self.repo = self.gh.get_repo(settings.github_repo)

    @staticmethod
    def _t(fn, *a, **k):
        return asyncio.to_thread(fn, *a, **k)

    async def head_sha(self, ref: str) -> str:
        return (await self._t(self.repo.get_git_ref, f"heads/{ref}")).object.sha

    async def create_branch(self, name: str) -> str:
        base_sha = await self.head_sha(self.base)
        await self._t(self.repo.create_git_ref, f"refs/heads/{name}", base_sha)
        return base_sha

    async def commit_files(self, branch, files, message, author_name, author_email) -> CommitRef:
        def work() -> CommitRef:
            ref = self.repo.get_git_ref(f"heads/{branch}")
            parent = self.repo.get_git_commit(ref.object.sha)
            elements = []
            for path, content in files.items():
                blob = self.repo.create_git_blob(content, "utf-8")           # explicit blob per file
                elements.append(InputGitTreeElement(path, "100644", "blob", sha=blob.sha))
            tree = self.repo.create_git_tree(elements, parent.tree)
            when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            who = InputGitAuthor(author_name, author_email, when)
            commit = self.repo.create_git_commit(message, tree, [parent], author=who, committer=who)
            ref.edit(commit.sha)
            return CommitRef(branch=branch, sha=commit.sha, author_name=author_name,
                             author_email=author_email, files=sorted(files))
        return await asyncio.to_thread(work)

    async def open_pr(self, branch, title, body) -> PRRef:
        pr = await self._t(self.repo.create_pull, title=title, body=body, head=branch, base=self.base)
        return PRRef(number=pr.number, url=pr.html_url, branch=branch)

    async def comment(self, pr_number: int, body: str) -> None:
        pr = await self._t(self.repo.get_pull, pr_number)
        await self._t(pr.create_review, body=body, event="COMMENT")

    async def merge_pr(self, approval: ApprovalRequest) -> str:
        if approval.status != ApprovalStatus.approved:
            raise MergeNotApproved(f"approval {approval.id} is {approval.status.value}; merge refused")
        pr = await self._t(self.repo.get_pull, approval.pr.number)
        try:
            res = await self._t(pr.merge, commit_message=f"Merge {approval.pr.branch} (approved {approval.id})",
                                merge_method="merge")
        except GithubException as e:
            raise RuntimeError(f"GitHub refused the merge: {e.data}") from e
        if not res.merged:
            raise RuntimeError(f"merge not completed: {res.message}")
        return res.sha

    async def close_pr(self, pr_number: int, reason: str) -> None:
        pr = await self._t(self.repo.get_pull, pr_number)
        await self._t(pr.create_issue_comment, f"Rejected by human reviewer: {reason}")
        await self._t(pr.edit, state="closed")
