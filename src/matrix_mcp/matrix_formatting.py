from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.token import Token

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from markdown_it.renderer import RendererHTML
    from markdown_it.rules_inline import StateInline
    from markdown_it.utils import EnvType, OptionsDict

CUSTOM_HTML_FORMAT = "org.matrix.custom.html"

_MAX_FORMATTED_CONTENT_BYTES = 56_000
_PLAIN_BLOCK_TYPES = frozenset({"paragraph_open", "inline", "paragraph_close"})
_MATRIX_TO_SAFE = "@:._-"
_ALLOWED_LINK_SCHEMES = ("http://", "https://", "ftp://", "mailto:", "magnet:", "matrix:", "mxc://")
_LEADING_ASCII_WS_OR_CTRL = re.compile(r"^[\x00-\x20]+")
_MULTI_BLANK_LINES = re.compile(r"\n{2,}")


class _MatrixMarkdownIt(MarkdownIt):
    def validateLink(self, url: str) -> bool:  # noqa: N802 - Overrides markdown-it method.
        return _normalize_url_prefix(url).startswith(_ALLOWED_LINK_SCHEMES)


def _normalize_url_prefix(url: str) -> str:
    return _LEADING_ASCII_WS_OR_CTRL.sub("", url).lower()


def _render_matrix_image(
    renderer: RendererHTML,
    tokens: Sequence[Token],
    idx: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    token = tokens[idx]
    raw_src = token.attrGet("src")
    src = raw_src if isinstance(raw_src, str) else ""
    scheme_src = _normalize_url_prefix(src)
    if scheme_src.startswith("mxc://"):
        return renderer.image(tokens, idx, options, env)
    alt = renderer.renderInlineAsText(token.children, options, env) if token.children else ""
    label = escapeHtml(alt or src)
    inside_link = (
        sum(
            1 if t.type == "link_open" else -1 if t.type == "link_close" else 0
            for t in tokens[:idx]
        )
        > 0
    )
    if scheme_src.startswith(("http://", "https://")) and not inside_link:
        return f'<a href="{escapeHtml(src)}">{label}</a>'
    return label


def _suppress_mention_delimiters(state: StateInline) -> None:
    pattern = state.env.get("mention_pattern")
    if not isinstance(pattern, re.Pattern) or not state.delimiters:
        return
    suppressed: set[int] = set()
    link_depth = 0
    idx = 0
    tokens = state.tokens
    while idx < len(tokens):
        token = tokens[idx]
        if token.type == "text" and link_depth == 0:
            run: list[tuple[int, int, int]] = []
            offset = 0
            while idx < len(tokens) and tokens[idx].type == "text":
                end = offset + len(tokens[idx].content)
                run.append((idx, offset, end))
                offset = end
                idx += 1
            joined = "".join(tokens[i].content for i, _, _ in run)
            for match in pattern.finditer(joined):
                m_start, m_end = match.span()
                suppressed.update(i for i, start, end in run if start < m_end and end > m_start)
            continue
        if token.type == "link_open":
            link_depth += 1
        elif token.type == "link_close":
            link_depth = max(0, link_depth - 1)
        idx += 1
    for delimiter in state.delimiters:
        if delimiter.token in suppressed:
            delimiter.open = False
            delimiter.close = False


def _build_markdown_parser() -> MarkdownIt:
    md = _MatrixMarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": True}).enable(
        ["table", "strikethrough", "linkify"]
    )
    if md.linkify is not None:
        md.linkify.set({"fuzzy_link": False, "fuzzy_email": False, "fuzzy_ip": False})
    md.inline.ruler2.before(
        "balance_pairs", "matrix_mention_delimiters", _suppress_mention_delimiters
    )
    md.add_render_rule("image", _render_matrix_image)
    return md


_MARKDOWN = _build_markdown_parser()


def render_markdown_html(body: str, *, mentions: Sequence[str] | None = None) -> str | None:
    """Render Markdown to Matrix HTML, returning None for pure plain text."""
    pattern = _compile_mention_pattern(mentions)
    env: dict[str, Any] = {"mention_pattern": pattern} if pattern is not None else {}
    tokens = _MARKDOWN.parse(body, env)
    if not tokens:
        return None
    _rewrite_inline_tokens(tokens, pattern)
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
    if "formatted_body" in content and _encoded_json_size(content) > _MAX_FORMATTED_CONTENT_BYTES:
        content.pop("format", None)
        content.pop("formatted_body", None)
    return content


def build_edit_message_content(
    body: str,
    *,
    event_id: str,
    msgtype: str = "m.text",
) -> dict[str, Any]:
    """Build an m.replace edit payload, dropping HTML if it exceeds the Matrix size budget."""
    new_content = build_text_message_content(body, msgtype=msgtype)
    replacement: dict[str, Any] = {
        "msgtype": msgtype,
        "body": f"* {body}",
        "m.new_content": new_content,
        "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
    }
    if "formatted_body" in new_content:
        replacement["format"] = CUSTOM_HTML_FORMAT
        replacement["formatted_body"] = f"* {new_content['formatted_body']}"
        if _encoded_json_size(replacement) > _MAX_FORMATTED_CONTENT_BYTES:
            for payload in (replacement, new_content):
                payload.pop("format", None)
                payload.pop("formatted_body", None)
    return replacement


def _encoded_json_size(content: Mapping[str, Any]) -> int:
    return len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


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


def _rewrite_inline_tokens(tokens: Sequence[Token], pattern: re.Pattern[str] | None) -> None:
    for token in tokens:
        if token.type != "inline" or not token.children:
            continue
        unwrap_stack: list[bool] = []
        link_depth = 0
        updated: list[Token] = []
        for child in token.children:
            if child.type == "link_open":
                href = child.attrGet("href")
                is_mxc = isinstance(href, str) and _normalize_url_prefix(href).startswith("mxc://")
                unwrap_stack.append(is_mxc)
                if not is_mxc:
                    link_depth += 1
                    updated.append(child)
            elif child.type == "link_close":
                if unwrap_stack and not unwrap_stack.pop():
                    link_depth = max(0, link_depth - 1)
                    updated.append(child)
            elif child.type == "text" and pattern is not None and link_depth == 0:
                updated.extend(_linkify_text_token(child, pattern))
            else:
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
    normalized = body.replace("\r\n", "\n").replace("\r", "\n").strip(" \t\n")
    lines = [line.strip(" \t") for line in normalized.split("\n")]
    return "\n\n".join(paragraph_texts) == _MULTI_BLANK_LINES.sub("\n\n", "\n".join(lines))
