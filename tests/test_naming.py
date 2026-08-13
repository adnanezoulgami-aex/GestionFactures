"""Tests du nettoyage et de l'unicité des noms de fichiers."""

from __future__ import annotations

import pytest

from factures.naming import FALLBACK_STEM, sanitize_filename, unique_filename


class TestSanitize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("GRAND TANGER IMMO", "GRAND TANGER IMMO"),
            ("Hôtel Nord Pinus Tanger", "Hôtel Nord Pinus Tanger"),
            ("O & M CONCEPT", "O & M CONCEPT"),
            ("CHÊNE , LOTI", "CHÊNE , LOTI"),
            ("OMKA BUILDING (ELHAMSS)", "OMKA BUILDING (ELHAMSS)"),
        ],
    )
    def test_valid_names_are_preserved(self, raw: str, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("A/B", "A-B"),
            (r"A\B", "A-B"),
            ("A:B", "A-B"),
            ('A"B', "A-B"),
            ("A<B>C", "A-B-C"),
            ("A|B?C*D", "A-B-C-D"),
        ],
    )
    def test_forbidden_characters_are_replaced(self, raw: str, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    def test_control_characters_are_removed(self) -> None:
        assert sanitize_filename("AB\x00\x1fCD") == "ABCD"

    def test_whitespace_is_collapsed_and_trimmed(self) -> None:
        assert sanitize_filename("  AQUILA   LOGISTICS \n") == "AQUILA LOGISTICS"

    def test_trailing_dots_and_spaces_are_dropped(self) -> None:
        assert sanitize_filename("SARL SA. ") == "SARL SA"

    @pytest.mark.parametrize("raw", ["", "   ", "///", "\x00"])
    def test_empty_results_fall_back(self, raw: str) -> None:
        assert sanitize_filename(raw) == FALLBACK_STEM

    @pytest.mark.parametrize("reserved", ["CON", "prn", "NUL", "COM1", "lpt9"])
    def test_reserved_device_names_are_escaped(self, reserved: str) -> None:
        assert sanitize_filename(reserved) == f"_{reserved}"

    def test_long_names_are_truncated(self) -> None:
        result = sanitize_filename("X" * 400)
        assert len(result) == 120

    def test_truncation_never_leaves_a_trailing_space(self) -> None:
        result = sanitize_filename("Y" * 119 + " " + "Z" * 50)
        assert not result.endswith(" ")

    def test_invalid_max_length_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            sanitize_filename("CLIENT", max_length=0)

    def test_result_is_usable_as_a_path_segment(self, tmp_path) -> None:
        target = tmp_path / f"{sanitize_filename('AB/CD: EF*')}.pdf"
        target.write_bytes(b"%PDF-1.7")
        assert target.exists()


class TestUnique:
    def test_free_name_is_returned_as_is(self) -> None:
        assert unique_filename("DRAKE.pdf", []) == "DRAKE.pdf"

    def test_collision_gets_a_counter(self) -> None:
        assert unique_filename("DRAKE.pdf", ["DRAKE.pdf"]) == "DRAKE (2).pdf"

    def test_counter_skips_names_already_taken(self) -> None:
        taken = ["DRAKE.pdf", "DRAKE (2).pdf"]
        assert unique_filename("DRAKE.pdf", taken) == "DRAKE (3).pdf"

    def test_comparison_ignores_case(self) -> None:
        assert unique_filename("Drake.pdf", ["DRAKE.PDF"]) == "Drake (2).pdf"

    def test_names_without_extension(self) -> None:
        assert unique_filename("DRAKE", ["drake"]) == "DRAKE (2)"

    def test_repeated_use_produces_distinct_names(self) -> None:
        taken: list[str] = []
        for _ in range(5):
            taken.append(unique_filename("SAMAVA.pdf", taken))
        assert len(set(name.casefold() for name in taken)) == 5
