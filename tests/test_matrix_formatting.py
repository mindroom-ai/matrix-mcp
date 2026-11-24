from __future__ import annotations

import pytest

from matrix_mcp.matrix_formatting import (
    CUSTOM_HTML_FORMAT,
    build_text_message_content,
    render_markdown_html,
)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "hi",
        "line 1\nline 2",
        "line 1\r\nline 2",
        "para 1\n\npara 2",
        "a < b & c > d",
    ],
)
def test_plain_text_returns_none(body: str) -> None:
    assert render_markdown_html(body) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("**bold**", "<p><strong>bold</strong></p>"),
        ("*italic*", "<p><em>italic</em></p>"),
        ("`code`", "<p><code>code</code></p>"),
        (
            "```python\nprint(1)\n```",
            '<pre><code class="language-python">print(1)\n</code></pre>',
        ),
        ("# Title", "<h1>Title</h1>"),
        ("- one\n- two", "<ul>\n<li>one</li>\n<li>two</li>\n</ul>"),
        ("1. one\n2. two", "<ol>\n<li>one</li>\n<li>two</li>\n</ol>"),
        ("> quoted", "<blockquote>\n<p>quoted</p>\n</blockquote>"),
        ("---", "<hr />"),
        (
            "| a | b |\n| --- | --- |\n| 1 | 2 |",
            (
                "<table>\n<thead>\n<tr>\n<th>a</th>\n<th>b</th>\n</tr>\n</thead>\n"
                "<tbody>\n<tr>\n<td>1</td>\n<td>2</td>\n</tr>\n</tbody>\n</table>"
            ),
        ),
        ("~~gone~~", "<p><s>gone</s></p>"),
        ("[docs](https://example.com)", '<p><a href="https://example.com">docs</a></p>'),
        (r"\*literal\*", "<p>*literal*</p>"),
        ("&amp;", "<p>&amp;</p>"),
        ("line 1  \nline 2", "<p>line 1<br />\nline 2</p>"),
        ("**title**\nsecond line", "<p><strong>title</strong><br />\nsecond line</p>"),
    ],
)
def test_markdown_constructs_render_to_html(body: str, expected: str) -> None:
    assert render_markdown_html(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "hi @alice:example.com",
            '<p>hi <a href="https://matrix.to/#/@alice:example.com">@alice:example.com</a></p>',
        ),
        (
            "hi @alice:example.com.",
            '<p>hi <a href="https://matrix.to/#/@alice:example.com">@alice:example.com</a>.</p>',
        ),
        (
            "hi @alice:example.com, welcome",
            (
                '<p>hi <a href="https://matrix.to/#/@alice:example.com">@alice:example.com</a>,'
                " welcome</p>"
            ),
        ),
        (
            "@alice:example.com: hi",
            '<p><a href="https://matrix.to/#/@alice:example.com">@alice:example.com</a>: hi</p>',
        ),
        (
            "(@alice:example.com)",
            '<p>(<a href="https://matrix.to/#/@alice:example.com">@alice:example.com</a>)</p>',
        ),
    ],
)
def test_mention_linkification_and_punctuation(body: str, expected: str) -> None:
    assert render_markdown_html(body, mentions=["@alice:example.com"]) == expected


@pytest.mark.parametrize(
    "body",
    [
        "email@alice:example.com",
        "@alice:example.com.evil",
        "@alice:example.com:8448",
        "@alice:example.com2",
        "hi @bob:example.com",
    ],
)
def test_mention_boundaries_and_unmentioned_users_not_linkified(body: str) -> None:
    assert render_markdown_html(body, mentions=["@alice:example.com"]) is None


def test_mention_longest_first_overlap_and_unmentioned_sibling() -> None:
    mentions = ["@alice:example.com", "@alice:example.com:8448"]
    html = render_markdown_html(
        "ping @alice:example.com:8448 and @bob:example.com",
        mentions=mentions,
    )
    assert html == (
        '<p>ping <a href="https://matrix.to/#/@alice:example.com:8448">'
        "@alice:example.com:8448</a> and @bob:example.com</p>"
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("`@alice:example.com`", "<p><code>@alice:example.com</code></p>"),
        ("```\n@alice:example.com\n```", "<pre><code>@alice:example.com\n</code></pre>"),
        ("    @alice:example.com", "<pre><code>@alice:example.com\n</code></pre>"),
        (
            "[@alice:example.com](https://example.com)",
            '<p><a href="https://example.com">@alice:example.com</a></p>',
        ),
        (
            "[**@alice:example.com**](https://example.com)",
            '<p><a href="https://example.com"><strong>@alice:example.com</strong></a></p>',
        ),
    ],
)
def test_mentions_skipped_in_code_and_existing_links(body: str, expected: str) -> None:
    assert render_markdown_html(body, mentions=["@alice:example.com"]) == expected


def test_security_escapes_raw_html_and_rejects_javascript_links() -> None:
    assert render_markdown_html("<script>alert(1)</script>") is None
    assert render_markdown_html("[click](javascript:alert(1))") is None

    script_html = render_markdown_html("**hi** <script>alert(1)</script>")
    assert script_html == "<p><strong>hi</strong> &lt;script&gt;alert(1)&lt;/script&gt;</p>"

    img_html = render_markdown_html("**hi** <img src=x onerror=alert(1)>")
    assert img_html == "<p><strong>hi</strong> &lt;img src=x onerror=alert(1)&gt;</p>"

    js_link_html = render_markdown_html("**hi** [click](javascript:alert(1))")
    assert js_link_html == "<p><strong>hi</strong> [click](javascript:alert(1))</p>"
    assert "href=" not in js_link_html


def test_build_text_message_content_handles_plain_and_formatted_payloads() -> None:
    plain = build_text_message_content("hello", mentions=[])
    assert plain == {
        "msgtype": "m.text",
        "body": "hello",
        "m.mentions": {"user_ids": []},
    }

    formatted = build_text_message_content(
        "Hello **@alice:example.com**",
        mentions=["@alice:example.com"],
        relates_to={"rel_type": "m.thread", "event_id": "$root"},
    )
    assert formatted == {
        "msgtype": "m.text",
        "body": "Hello **@alice:example.com**",
        "format": CUSTOM_HTML_FORMAT,
        "formatted_body": (
            '<p>Hello <strong><a href="https://matrix.to/#/@alice:example.com">'
            "@alice:example.com</a></strong></p>"
        ),
        "m.mentions": {"user_ids": ["@alice:example.com"]},
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root"},
    }
