"""Where updates come from.

The updater pulled ``origin main`` whatever was checked out. On any other
branch that is not an update, it is a merge of main into your work — and with
the hard reset in front of it, the branch you were running becomes main with
extra steps. A checkout already records where it came from: the branch's
configured remote and merge ref are exactly what a bare ``git pull`` would
use, and that is what is resolved here.

Everything runs git as the account that owns the checkout. The service runs as
root, and root writing into a user-owned ``.git`` leaves files that the user's
own git then will not touch.
"""

import logging
import os
import re
import subprocess

from splitflap.settings import REPO_DIR

GIT_TIMEOUT = 30

# Falls back to the upstream project when the remote is not a GitHub URL, so
# release notes still resolve for a checkout cloned some other way.
DEFAULT_REPO = 'csader/splitflap-os'

GITHUB_URL_RE = re.compile(
    r'^(?:(?:https?|git|ssh)://)?(?:[^@/]+@)?github\.com[:/]+'
    r'(?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$',
    re.IGNORECASE,
)


def parse_github_repo(url):
    """``owner/name`` for a GitHub remote, in any of the forms git writes."""
    match = GITHUB_URL_RE.match(str(url or '').strip())
    return f"{match.group('owner')}/{match.group('name')}" if match else None


def _repo_user():
    """The account that owns the checkout, or None where that has no meaning."""
    if os.name != 'posix':
        return None
    try:
        import pwd
        return pwd.getpwuid(os.stat(REPO_DIR).st_uid).pw_name
    except Exception:
        return None


def git(*args, timeout=GIT_TIMEOUT):
    """Run one git command in the checkout. Returns ``(ok, output)``.

    Output is stdout on success and whatever git complained about on failure,
    so a caller can put it in front of a person either way.
    """
    args = [str(arg) for arg in args]
    user = _repo_user()
    command = (['sudo', '-u', user] if user else []) + ['git'] + args
    try:
        result = subprocess.run(
            command, cwd=REPO_DIR, timeout=timeout, capture_output=True, text=True)
    except Exception as exc:
        logging.error("git %s could not run: %s", " ".join(args), exc)
        return False, str(exc)

    stdout = (result.stdout or "").strip()
    if result.returncode == 0:
        return True, stdout
    return False, (result.stderr or "").strip() or stdout or "git {} failed".format(args[0])


def update_target():
    """Which remote and branch this checkout updates from.

    A detached HEAD has no answer. Saying so beats guessing main and pulling
    someone else's branch over the one they are running.
    """
    blank = {"branch": None, "remote": None, "remote_branch": None,
             "detached": True, "url": None, "repo": DEFAULT_REPO}

    ok, branch = git('rev-parse', '--abbrev-ref', 'HEAD', timeout=10)
    if not ok or not branch or branch == 'HEAD':
        return blank

    ok, remote = git('config', '--get', f'branch.{branch}.remote', timeout=10)
    remote = remote if ok and remote else 'origin'

    # A branch can be pushed under a different name than it has locally, and
    # branch.<name>.merge is the only place that is written down.
    ok, merge_ref = git('config', '--get', f'branch.{branch}.merge', timeout=10)
    remote_branch = re.sub(r'^refs/heads/', '', merge_ref) if ok and merge_ref else branch

    ok, url = git('remote', 'get-url', remote, timeout=10)
    url = url if ok and url else None

    return {
        "branch": branch,
        "remote": remote,
        "remote_branch": remote_branch,
        "detached": False,
        "url": url,
        "repo": parse_github_repo(url) or DEFAULT_REPO,
    }


def describe(target):
    """``origin/main``, for showing a person which branch is being tracked."""
    if target.get("detached") or not target.get("remote"):
        return ""
    return "{}/{}".format(target["remote"], target["remote_branch"])


def commits_behind(target):
    """How many commits the tracked branch has that we do not, or None.

    None means git could not answer — no remote, no network, a branch that
    does not exist upstream — which is different from being up to date, and
    the caller has to tell them apart.
    """
    if target.get("detached") or not target.get("remote"):
        return None
    ok, output = git('fetch', target["remote"], target["remote_branch"], timeout=60)
    if not ok:
        logging.warning("Could not fetch %s: %s", describe(target), output)
        return None
    ok, count = git('rev-list', '--count', 'HEAD..FETCH_HEAD', timeout=20)
    if not ok:
        return None
    try:
        return int(count)
    except ValueError:
        return None


def pull(target):
    """Bring the checkout up to date with the branch it tracks.

    Local changes are discarded first: this runs on a display, not a
    workstation, and an edit made on the Pi is not worth failing an update
    over. It is the same reset the updater has always done — what changed is
    that what follows it is the branch you are on.
    """
    if target.get("detached") or not target.get("remote"):
        return False, "This checkout is not on a branch, so there is nothing to update from."

    # git refuses to operate on a directory owned by someone else, and the
    # service is not the owner.
    subprocess.run(
        ['git', 'config', '--global', '--add', 'safe.directory',
         os.path.realpath(REPO_DIR)],
        timeout=5, capture_output=True,
    )

    git('reset', '--hard', 'HEAD')
    ok, output = git('pull', target["remote"], target["remote_branch"], timeout=60)
    if not ok:
        logging.error("Update pull from %s failed: %s", describe(target), output)
    return ok, output
