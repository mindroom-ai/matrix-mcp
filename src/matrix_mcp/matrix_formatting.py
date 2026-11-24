from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.token import Token

if TYPE_CHECKING:
    from collections.abc import Sequence

CUSTOM_HTML_FORMAT = "org.matrix.custom.html"

_MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(
    ["table", "strikethrough"]
)
_PLAIN_BLOCK_TYPES = frozenset({"paragraph_open", "inline", "paragraph_close"})
_MATRIX_TO_SAFE = "@:._-"


def render_markdown_html(body: str, *, mentions: Sequence[str] | None = None) -> str | None:
    """Render Markdown to Matrix HTML, returning None for pure plain text."""
    env: dict[str, Any] = {}
    tokens = _MARKDOWN.parse(body, env)
    if not tokens:
        return None
    pattern = _compile_mention_pattern(mentions)
    if pattern is not None:
        _linkify_tokens(tokens, pattern)
    if _is_plain_text(tokens, body):
        return None
    html = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, env).strip()
    return html or None


def build_text_message_content(
    body: str,
    *,
    msgtype: str = "m.text",
    mentions: Sequence[str] | None = None,
    relates_to: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an m.room.message payload with optional Markdown HTML and mentions."""
    content: dict[str, Any] = {"msgtype": msgtype, "body": body}
    formatted_body = render_markdown_html(body, mentions=mentions)
    if formatted_body is not None:
        content["format"] = CUSTOM_HTML_FORMAT
        content["formatted_body"] = formatted_body
    if mentions is not None:
        content["m.mentions"] = {"user_ids": list(mentions)}
    if relates_to is not None:
        content["m.relates_to"] = relates_to
    return content


def _compile_mention_pattern(mentions: Sequence[str] | None) -> re.Pattern[str] | None:
    unique = sorted(
        {mention for mention in mentions or () if mention},
        key=lambda user_id: (-len(user_id), user_id),
    )
    if not unique:
        return None
    alternation = "|".join(re.escape(user_id) for user_id in unique)
    return re.compile(
        rf"(?<![A-Za-z0-9._=/+-])({alternation})(?![A-Za-z0-9-]|(?:\.[A-Za-z0-9-])|(?::[0-9]))"
    )


def _linkify_tokens(tokens: Sequence[Token], pattern: re.Pattern[str]) -> None:
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        link_depth = 0
        updated: list[Token] = []
        for child in token.children:
            if child.type == "text" and link_depth == 0:
                updated.extend(_linkify_text_token(child, pattern))
                continue
            if child.type == "link_open":
                link_depth += 1
            elif child.type == "link_close":
                link_depth = max(0, link_depth - 1)
            updated.append(child)
        token.children = updated


def _linkify_text_token(token: Token, pattern: re.Pattern[str]) -> list[Token]:
    text = token.content
    matches = list(pattern.finditer(text)) if text else []
    if not matches:
        return [token]
    result: list[Token] = []
    pos = 0
    for match in matches:
        start, end = match.span()
        if start > pos:
            result.append(Token("text", "", 0, content=text[pos:start]))
        user_id = match.group(1)
        link_open = Token("link_open", "a", 1)
        link_open.attrSet("href", f"https://matrix.to/#/{quote(user_id, safe=_MATRIX_TO_SAFE)}")
        result.extend(
            [link_open, Token("text", "", 0, content=user_id), Token("link_close", "a", -1)]
        )
        pos = end
    if pos < len(text):
        result.append(Token("text", "", 0, content=text[pos:]))
    return result


def _is_plain_text(tokens: Sequence[Token], body: str) -> bool:
    paragraph_texts: list[str] = []
    for token in tokens:
        if token.type not in _PLAIN_BLOCK_TYPES:
            return False
        if token.type != "inline":
            continue
        parts: list[str] = []
        for child in token.children or ():
            if child.type == "text":
                parts.append(child.content)
            elif child.type == "softbreak":
                parts.append("\n")
            else:
                return False
        paragraph_texts.append("".join(parts))
    return "\n\n".join(paragraph_texts) == body.replace("\r\n", "\n").replace("\r", "\n")
