from __future__ import annotations

import html
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

CSS = """body { font-family: Georgia, serif; line-height: 1.65; margin: 5%; }
h1, h2 { text-align: center; margin: 1.4em 0 1em; line-height: 1.3; }
p { text-indent: 1.2em; margin: 0.5em 0; }
"""


def build_epub(
    *,
    title: str,
    author: str,
    chapters: list[dict[str, str]],
    out_path: Path,
    cover_bytes: bytes | None = None,
    cover_ext: str = ".jpg",
    lang: str = "en",
) -> Path:
    """Compile downloaded chapters into a valid EPUB 3 archive with TOC and optional cover."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    book_id = f"urn:uuid:{uuid.uuid4()}"
    safe_title = html.escape(title)
    safe_author = html.escape(author or "Unknown")

    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine_items: list[str] = []
    nav_li: list[str] = []
    ncx_points: list[str] = []

    has_cover = bool(cover_bytes)
    cover_mime = "image/png" if cover_ext.lower() == ".png" else "image/jpeg"
    cover_file = f"cover{cover_ext.lower()}"

    if has_cover:
        manifest_items.append(
            f'<item id="cover-image" href="{cover_file}" media-type="{cover_mime}" properties="cover-image"/>'
        )
        manifest_items.append('<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        spine_items.append('<itemref idref="cover"/>')

    with zipfile.ZipFile(out_path, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
            "</container>",
            compress_type=zipfile.ZIP_DEFLATED,
        )
        zf.writestr("OEBPS/style.css", CSS.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

        if has_cover and cover_bytes:
            zf.writestr(f"OEBPS/{cover_file}", cover_bytes, compress_type=zipfile.ZIP_DEFLATED)
            cover_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title></head>'
                f'<body style="margin:0;text-align:center;"><img src="{cover_file}" alt="Cover" style="max-width:100%;height:auto;"/></body></html>'
            )
            zf.writestr("OEBPS/cover.xhtml", cover_xhtml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

        for idx, ch in enumerate(chapters, start=1):
            cid = f"ch_{idx:04d}"
            fname = f"{cid}.xhtml"
            ctitle = html.escape(ch.get("title") or f"Chapter {idx}")
            body = ch.get("body_html") or "<p>(Empty chapter)</p>"

            ch_xhtml = (
                '<?xml version="1.0" encoding="utf-8"?>\n'
                f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">\n'
                f'<head><title>{ctitle}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
                f"<body><h1>{ctitle}</h1>\n{body}\n</body></html>"
            )
            zf.writestr(f"OEBPS/{fname}", ch_xhtml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
            manifest_items.append(f'<item id="{cid}" href="{fname}" media-type="application/xhtml+xml"/>')
            spine_items.append(f'<itemref idref="{cid}"/>')
            nav_li.append(f'<li><a href="{fname}">{ctitle}</a></li>')
            ncx_points.append(
                f'<navPoint id="np_{idx}" playOrder="{idx}"><navLabel><text>{ctitle}</text></navLabel><content src="{fname}"/></navPoint>'
            )

        nav_xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
            f'<head><title>{safe_title}</title></head>\n'
            f'<body><nav epub:type="toc"><h1>{safe_title}</h1><ol>\n'
            + "\n".join(nav_li)
            + "\n</ol></nav></body></html>"
        )
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

        toc_ncx = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'<head><meta name="dtb:uid" content="{book_id}"/></head>\n'
            f"<docTitle><text>{safe_title}</text></docTitle>\n<navMap>\n"
            + "\n".join(ncx_points)
            + "\n</navMap></ncx>"
        )
        zf.writestr("OEBPS/toc.ncx", toc_ncx.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

        content_opf = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            f'  <dc:identifier id="bookid">{book_id}</dc:identifier>\n'
            f"  <dc:title>{safe_title}</dc:title>\n"
            f"  <dc:creator>{safe_author}</dc:creator>\n"
            f"  <dc:language>{lang}</dc:language>\n"
            "</metadata>\n"
            "<manifest>\n  " + "\n  ".join(manifest_items) + "\n</manifest>\n"
            '<spine toc="ncx">\n  ' + "\n  ".join(spine_items) + "\n</spine>\n"
            "</package>"
        )
        zf.writestr("OEBPS/content.opf", content_opf.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)

    return out_path


def build_txt(*, title: str, author: str, chapters: list[dict[str, str]], out_path: Path) -> Path:
    """Compile downloaded chapters into a clean UTF-8 TXT file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"{title}\nAuthor: {author}\n{'=' * 40}\n"]
    tag_re = re.compile(r"<[^>]+>")
    for ch in chapters:
        parts.append(f"\n\n### {ch.get('title', 'Chapter')}\n\n")
        plain = tag_re.sub("\n", ch.get("body_html", ""))
        plain = html.unescape(re.sub(r"\n{3,}", "\n\n", plain)).strip()
        parts.append(plain)
    out_path.write_text("".join(parts), encoding="utf-8")
    return out_path
