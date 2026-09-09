"""Fail a release job when the GitHub default branch has no required checks."""
import json
import os
import sys
from urllib.request import Request, urlopen


def main():
    repository = os.getenv('GITHUB_REPOSITORY')
    token = os.getenv('GITHUB_TOKEN')
    branch = os.getenv('GITHUB_DEFAULT_BRANCH', 'main')
    if not repository or not token:
        print('GITHUB_REPOSITORY and GITHUB_TOKEN are required', file=sys.stderr)
        return 2
    req = Request(f'https://api.github.com/repos/{repository}/branches/{branch}/protection', headers={'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {token}', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urlopen(req, timeout=10) as response:
            data = json.load(response)
    except Exception as exc:
        print(f'branch protection lookup failed: {type(exc).__name__}', file=sys.stderr)
        return 1
    checks = ((data.get('required_status_checks') or {}).get('contexts') or [])
    if not data.get('required_pull_request_reviews') or not checks:
        print(f'{branch} must require pull request reviews and CI checks', file=sys.stderr)
        return 1
    print(f'{branch} protection enabled with {len(checks)} required checks')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
