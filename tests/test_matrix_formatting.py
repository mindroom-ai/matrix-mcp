from __future__ import annotations

import pytest

from matrix_mcp.matrix_formatting import (
    CUSTOM_HTML_FORMAT,
    build_edit_message_content,
    build_text_message_content,
    render_markdown_html,
)


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   ",
        "hi",
        "  hello  ",
        "line 1\nline 2",
        "line 1\r\nline 2",
        "para 1\n\npara 2",
        "one\n\n\ntwo",
        "one\n  \n  \ntwo",
        "Agenda:\n  Alice: testing\n  Bob: release",
        "a < b & c > d",
        "Edit main.py and README.md",
        "Run script.sh on 192.168.1.1",
        "user@example.com",
        "//example.com/foo",
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
        ("    code", "<pre><code>code\n</code></pre>"),
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
        (r"Use \*stars\* literally", "<p>Use *stars* literally</p>"),
        ("a &amp; b", "<p>a &amp; b</p>"),
        ("line 1  \nline 2", "<p>line 1<br />\nline 2</p>"),
        ("**title**\nsecond line", "<p><strong>title</strong><br />\nsecond line</p>"),
        (
            "https://github.com/org/repo/blob/main/src/pkg/__init__.py",
            (
                '<p><a href="https://github.com/org/repo/blob/main/src/pkg/__init__.py">'
                "https://github.com/org/repo/blob/main/src/pkg/__init__.py</a></p>"
            ),
        ),
        (
            "https://en.wikipedia.org/wiki/_foo_",
            '<p><a href="https://en.wikipedia.org/wiki/_foo_">https://en.wikipedia.org/wiki/_foo_</a></p>',
        ),
    ],
)
def test_markdown_constructs_render_to_html(body: str, expected: str) -> None:
    assert render_markdown_html(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "[ok](http://example.com)",
            '<p><a href="http://example.com">ok</a></p>',
        ),
        (
            "[ok](ftp://example.com/a)",
            '<p><a href="ftp://example.com/a">ok</a></p>',
        ),
        (
            "[ok](mailto:alice@example.com)",
            '<p><a href="mailto:alice@example.com">ok</a></p>',
        ),
        (
            "[ok](magnet:?xt=urn:btih:1234)",
            '<p><a href="magnet:?xt=urn:btih:1234">ok</a></p>',
        ),
        (
            "[ok](matrix:u/alice:example.com)",
            '<p><a href="matrix:u/alice:example.com">ok</a></p>',
        ),
        (
            "[x](mxc://example.com/abc)",
            "<p>x</p>",
        ),
        (
            "<mxc://example.com/abc>",
            "<p>mxc://example.com/abc</p>",
        ),
        (
            "![photo](mxc://example.com/abc)",
            '<p><img src="mxc://example.com/abc" alt="photo" /></p>',
        ),
        (
            "![plot](https://example.com/plot.png)",
            '<p><a href="https://example.com/plot.png">plot</a></p>',
        ),
        (
            "![](https://example.com/plot.png)",
            '<p><a href="https://example.com/plot.png">https://example.com/plot.png</a></p>',
        ),
        (
            "[![badge](https://example.com/badge.svg)](https://example.com/build)",
            '<p><a href="https://example.com/build">badge</a></p>',
        ),
        (
            "![pic](ftp://example.com/pic.png)",
            "<p>pic</p>",
        ),
        (
            "![](ftp://example.com/pic.png)",
            "<p>ftp://example.com/pic.png</p>",
        ),
        (
            "![x](mailto:alice@example.com)",
            "<p>x</p>",
        ),
    ],
)
def test_matrix_link_and_image_schemes(body: str, expected: str) -> None:
    assert render_markdown_html(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "[manual](./README.md)",
        "[root](/foo)",
        "[sample](data:image/png;base64,AA==)",
        "[click](javascript:alert(1))",
        "[click](vbscript:msgbox(1))",
        "[local](file:///etc/passwd)",
        "![x](data:image/png;base64,AA==)",
    ],
)
def test_disallowed_link_and_image_schemes_stay_plain_text(body: str) -> None:
    assert render_markdown_html(body) is None
    formatted = render_markdown_html(f"**hi** {body}")
    assert formatted is not None
    assert "<a " not in formatted
    assert "<img " not in formatted


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


def test_mentions_with_underscores_are_preserved() -> None:
    mentions = ["@_ops_:example.org"]
    assert render_markdown_html("Hi @_ops_:example.org", mentions=mentions) == (
        '<p>Hi <a href="https://matrix.to/#/@_ops_:example.org">@_ops_:example.org</a></p>'
    )
    assert render_markdown_html("**@_ops_:example.org**", mentions=mentions) == (
        '<p><strong><a href="https://matrix.to/#/@_ops_:example.org">'
        "@_ops_:example.org</a></strong></p>"
    )
    assert render_markdown_html("Hi @_ops_:example.org and _italic_", mentions=mentions) == (
        '<p>Hi <a href="https://matrix.to/#/@_ops_:example.org">@_ops_:example.org</a>'
        " and <em>italic</em></p>"
    )
    linked = render_markdown_html(
        "[link @_ops_:example.org](https://example.com)", mentions=mentions
    )
    assert linked is not None
    assert linked.startswith('<p><a href="https://example.com">')
    assert linked.count("<a ") == 1
    assert "matrix.to" not in linked
    assert render_markdown_html("`@_ops_:example.org`", mentions=mentions) == (
        "<p><code>@_ops_:example.org</code></p>"
    )
    assert render_markdown_html(
        "Hi @_a:example.org and @_b:example.org",
        mentions=["@_a:example.org", "@_b:example.org"],
    ) == (
        '<p>Hi <a href="https://matrix.to/#/@_a:example.org">@_a:example.org</a>'
        ' and <a href="https://matrix.to/#/@_b:example.org">@_b:example.org</a></p>'
    )


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


def test_build_text_message_content_handles_plain_formatted_and_size_limit() -> None:
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

    oversized_body = "**a** " * 6_000
    oversized = build_text_message_content(
        oversized_body,
        mentions=["@alice:example.com"],
        relates_to={"rel_type": "m.thread", "event_id": "$root"},
    )
    assert "format" not in oversized
    assert "formatted_body" not in oversized
    assert oversized["body"] == oversized_body
    assert oversized["m.mentions"] == {"user_ids": ["@alice:example.com"]}
    assert oversized["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$root"}


def test_build_edit_message_content_drops_html_from_both_payloads_when_oversized() -> None:
    body = "**" + ("a" * 18_000) + "**"
    assert "formatted_body" in build_text_message_content(body)

    edit = build_edit_message_content(body, event_id="$target", msgtype="m.notice")
    assert edit == {
        "msgtype": "m.notice",
        "body": f"* {body}",
        "m.new_content": {"msgtype": "m.notice", "body": body},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
    }
