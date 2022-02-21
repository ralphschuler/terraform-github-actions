import json
import os
import re
from typing import Optional, Any

from github_actions.api import IssueUrl, GithubApi, CommentUrl
from github_actions.debug import debug

try:
    collapse_threshold = int(os.environ['TF_PLAN_COLLAPSE_LENGTH'])
except (ValueError, KeyError):
    collapse_threshold = 10


class TerraformComment:
    """
    Represents a Terraform PR comment

    The comment must have been successfully created for an object of this type to have been created.

    A Terraform PR comment has a number of elements that are formatted such that they can later be parsed back into
    an equivalent TerraformComment object.

    """

    def __init__(self, *, issue_url: IssueUrl, comment_url: Optional[CommentUrl], headers: dict[str, str], description: str, summary: str, body: str, status: str):
        self._issue_url = issue_url
        self._comment_url = comment_url
        self._headers = headers
        self._description = description
        self._summary = summary
        self._body = body
        self._status = status

    @property
    def comment_url(self) -> Optional[CommentUrl]:
        return self._comment_url

    @comment_url.setter
    def comment_url(self, comment_url: CommentUrl) -> None:
        if self._comment_url is not None:
            raise Exception('Can only set url for comments that don\'t exist yet')
        self._comment_url = comment_url

    @property
    def issue_url(self) -> IssueUrl:
        return self._issue_url

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    @property
    def description(self) -> str:
        return self._description

    @property
    def summary(self) -> str:
        return self._summary

    @property
    def body(self) -> str:
        return self._body

    @property
    def status(self) -> str:
        return self._status


def _format_comment_header(**kwargs) -> str:
    args = ' '.join(f'{k}={v}' for k, v in kwargs.items())
    return f'<!-- dflook/terraform-github-actions {args} -->'


def _parse_comment_header(comment_header: Optional[str]) -> dict[str, str]:
    if comment_header is None:
        return {}

    if header := re.match(r'^<!--\sdflook/terraform-github-actions\s(?P<args>.*?)-->', comment_header):
        return {arg['key']: arg['value'] for arg in re.finditer(r'(?P<key>\S+)=(?P<value>\S+)', header['args'])}

    return {}


def _from_api_payload(comment: dict[str, Any]) -> Optional[TerraformComment]:
    match = re.match(rf'''
            (?P<headers><!--.*?-->)?
            (?P<description>.*)
            <details>
            (?:<summary>(?P<summary>.*)?</summary>)?
            ```(?:hcl)?
            (?P<body>[.\s]*)
            ```
            </details>
            (?P<status>[.\s]*)
        ''',
                     comment['body'],
                     re.VERBOSE
                     )

    if not match:
        return None

    return TerraformComment(
        issue_url=comment['issue_url'],
        comment_url=comment['url'],
        headers=_parse_comment_header(match.group('headers')),
        description=match.group('description').strip(),
        summary=match.group('summary').strip(),
        body=match.group('body').strip(),
        status=match.group('status').strip()
    )


def _to_api_payload(comment: TerraformComment) -> str:
    details_open = False
    hcl_highlighting = False

    if comment.body.startswith('Error'):
        details_open = True
    elif 'Plan:' in comment.body:
        hcl_highlighting = True
        num_lines = len(comment.body.splitlines())
        if num_lines < collapse_threshold:
            details_open = True

    if comment.summary is None:
        details_open = True

    header = _format_comment_header(**comment.headers)

    body = f'''{header}{comment.description}
    <details{details_open}>
    {f'<summary>{comment.summary}</summary>' if comment.summary is not None else ''}

    ```{'hcl' if hcl_highlighting else ''}
    {comment.body}
    ```
    </details>
    '''

    if comment.status:
        body += '\n' + comment.status

    return body


def find_comment(github: GithubApi, issue_url: IssueUrl, username: str, headers: dict[str, str], legacy_description: str) -> TerraformComment:
    """
    Find a github comment that matches the given headers

    If no comment is found with the specified headers, tries to find a comment that matches the specified description instead.
    This is in case the comment was made with an earlier version, where comments were matched by description only.

    If not existing comment is found a new TerraformComment object is returned which represents a PR comment yet to be created.

    :param github: The github api object to make requests with
    :param issue_url: The issue to find the comment in
    :param username: The user who made the comment
    :param headers: The headers that must be present on the comment
    :param legacy_description: The description that must be present on the comment, if not headers are found.
    """

    backup_comment = None

    for comment_payload in github.paged_get(issue_url):
        if comment_payload['user']['login'] != username:
            continue

        debug(json.dumps(comment_payload))

        if comment := _from_api_payload(comment_payload):

            if comment.headers == headers:
                debug('Found comment that matches headers')
                return comment

            debug(f"Didn't match comment with {comment.headers=}")

            if comment.description == legacy_description:
                backup_comment = comment

            debug(f"Didn't match comment with {comment.description=}")

    if backup_comment is not None:
        debug('Found comment matching legacy description')
        return backup_comment

    debug('No matching comment exists')
    return TerraformComment(
        issue_url=issue_url,
        comment_url=None,
        headers=headers,
        description='',
        summary='',
        body='',
        status=''
    )


def update_comment(
    github: GithubApi,
    comment: TerraformComment,
    *,
    headers: dict[str, str] = None,
    description: str = None,
    summary: str = None,
    body: str = None,
    status: str = None
) -> TerraformComment:
    new_comment = TerraformComment(
        issue_url=comment.issue_url,
        comment_url=comment.comment_url,
        headers=headers if headers is not None else comment.headers,
        description=description if description is not None else comment.description,
        summary=summary if summary is not None else comment.summary,
        body=body if body is not None else comment.body,
        status=status if status is not None else comment.status
    )

    if comment.comment_url is not None:
        response = github.patch(comment.comment_url, json={'body': _to_api_payload(new_comment)})
        response.raise_for_status()
    else:
        response = github.post(comment.issue_url, json={'body': _to_api_payload(new_comment)})
        response.raise_for_status()
        new_comment.url = response.json()['url']

    return new_comment
