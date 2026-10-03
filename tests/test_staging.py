from __future__ import annotations

import unittest

from vlrelease import staging
from vlrelease.debchangelog import parse_top_entry, render_body
from vlrelease.markdown import shift_headings
from vlrelease.notes import parse_top_entry as parse_notes_entry
from vlrelease.notes import prepend_entry, render_entry
from datetime import date


class ChangelogNextTests(unittest.TestCase):
    def test_template_is_empty(self) -> None:
        self.assertEqual(staging.parse_changelog_next(staging.CHANGELOG_NEXT_TEMPLATE), [])
        self.assertFalse(staging.is_staged(staging.CHANGELOG_NEXT_TEMPLATE))

    def test_flat_bullets_with_continuations_and_children(self) -> None:
        sections = staging.parse_changelog_next(
            "- First change\n  continues here.\n  - detail one\n    more detail\n* Second change\n"
        )
        self.assertEqual(len(sections), 1)
        self.assertIsNone(sections[0].heading)
        first, second = sections[0].items
        self.assertEqual(first.text, "First change continues here.")
        self.assertEqual(first.children, ["detail one more detail"])
        self.assertEqual(second.text, "Second change")

    def test_sections(self) -> None:
        sections = staging.parse_changelog_next("## Security\n- Fix A\n\n## Packaging\n- Ship B\n")
        self.assertEqual([s.heading for s in sections], ["Security", "Packaging"])

    def test_rejections(self) -> None:
        cases = {
            "plain paragraph text\n": "expected '- bullet'",
            "# Title\n- x\n": "only '## Section'",
            "### Deep\n- x\n": "only '## Section'",
            "- ok\n## Later\n- y\n": "before the first",
            "## Empty\n": "has no bullets",
            "- TODO\n": "placeholder",
            "- <describe the change>\n": "placeholder",
            "  - orphan\n": "nested bullet without a parent",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(staging.StagingError, fragment):
                staging.parse_changelog_next(text)


class NotesNextTests(unittest.TestCase):
    def test_template_is_empty(self) -> None:
        self.assertIsNone(staging.parse_notes_next(staging.RELEASE_NOTES_NEXT_TEMPLATE))

    def test_title_and_body(self) -> None:
        notes = staging.parse_notes_next(staging.RELEASE_NOTES_NEXT_TEMPLATE + "\n# Big things\n\nBody text.\n")
        self.assertEqual(notes, staging.StagedNotes("Big things", "Body text."))

    def test_rejections(self) -> None:
        cases = {
            "Body without title\n": "first line must be '# <release title>'",
            "# TBD\n\nbody\n": "placeholder",
            "# v1.2.3 is here\n\nbody\n": "contains a version number",
            "# Title only\n": "body",
            "# Title\n\n# Another h1\n": "level-1",
            "# Title\n\n###### deep\n": "level-6",
        }
        for text, fragment in cases.items():
            with self.subTest(text=text), self.assertRaisesRegex(staging.StagingError, fragment):
                staging.parse_notes_next(text)

    def test_version_numbers_inside_title_are_fine(self) -> None:
        self.assertEqual(staging.parse_notes_next("# Drop the 1.2.0 API\n\nbody\n").title, "Drop the 1.2.0 API")


class RenderingTests(unittest.TestCase):
    def test_debian_body_wraps_and_keeps_code_spans_whole(self) -> None:
        sections = staging.parse_changelog_next(
            "- A very long change description that keeps going so that it has to wrap onto another line, "
            "mentioning `vlr prepare --dry-run` near the end of the sentence.\n"
        )
        lines = render_body(sections)
        self.assertTrue(all(len(line) <= 79 for line in lines))
        self.assertTrue(lines[0].startswith("  * "))
        self.assertTrue(all(line.startswith("    ") for line in lines[1:]))
        self.assertTrue(any("`vlr prepare --dry-run`" in line for line in lines))

    def test_grouped_rendering(self) -> None:
        sections = staging.parse_changelog_next("## Security\n- Fix A\n  - detail\n")
        self.assertEqual(render_body(sections), ["  * Security", "    - Fix A", "      + detail"])

    def test_heading_shift_ignores_code_fences(self) -> None:
        text = "## Section\n\n```sh\n# a shell comment\n```\n\n### Sub\n"
        shifted = shift_headings(text, 1)
        self.assertIn("### Section", shifted)
        self.assertIn("# a shell comment", shifted)
        self.assertEqual(shift_headings(shifted, -1), text)

    def test_notes_entry_roundtrip(self) -> None:
        body = "Intro.\n\n## Upgrading\n\n```\n# not a heading\n```"
        entry = render_entry(version="1.2.0", title="Title", released=date(2026, 10, 3), body=body)
        document = prepend_entry("", entry, project_name="Demo")
        parsed = parse_notes_entry(document)
        self.assertEqual((parsed.version, parsed.title, parsed.date), ("1.2.0", "Title", "2026-10-03"))
        self.assertEqual(parsed.body, body)
        self.assertIn("### Upgrading", document)

    def test_prepend_preserves_history_and_preamble(self) -> None:
        old = render_entry(version="1.0.0", title="Old", released=date(2026, 1, 1), body="Old body.")
        document = prepend_entry("# Demo release notes\n\nIntro paragraph.\n", old, project_name="Demo")
        new = render_entry(version="1.1.0", title="New", released=date(2026, 2, 1), body="New body.")
        updated = prepend_entry(document, new, project_name="Demo")
        self.assertTrue(updated.startswith("# Demo release notes\n\nIntro paragraph.\n\n<!-- vl-release:entry version=1.1.0 -->"))
        self.assertIn(old.strip(), updated)
        self.assertLess(updated.index("1.1.0"), updated.index("1.0.0"))

    def test_prepend_into_legacy_history_without_markers(self) -> None:
        legacy = "# Notes\n\n## 0.9.0\n\nlegacy entry\n"
        new = render_entry(version="1.0.0", title="New", released=date(2026, 2, 1), body="New body.")
        updated = prepend_entry(legacy, new, project_name="Demo")
        self.assertLess(updated.index("1.0.0"), updated.index("## 0.9.0"))
        self.assertTrue(updated.endswith("## 0.9.0\n\nlegacy entry\n"))

    def test_debian_parse_top_entry(self) -> None:
        text = (
            "demo (1.2.0-1) unstable; urgency=high\n\n  * Change.\n\n"
            " -- A Person <a@example.com>  Sat, 03 Oct 2026 10:00:00 +0000\n\n"
            "demo (1.1.0-1) unstable; urgency=medium\n\n  * Older.\n\n -- A Person <a@example.com>  Fri, 02 Oct 2026 10:00:00 +0000\n"
        )
        entry = parse_top_entry(text)
        self.assertEqual((entry.source, entry.version, entry.upstream, entry.revision), ("demo", "1.2.0-1", "1.2.0", "1"))
        self.assertEqual(entry.urgency, "high")


if __name__ == "__main__":
    unittest.main()
