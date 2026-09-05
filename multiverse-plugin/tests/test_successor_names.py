"""Naming a successor: the lineage carries on rather than starting over.

An agent that hands off is replaced, not duplicated, so its successor
keeps its name and takes the next generation — ``Quasar`` → ``Quasar II``
→ ``Quasar III``. Roman numerals, because a lineage is a succession and
``Quasar 2`` reads like a second copy of the same agent.
"""

from __future__ import annotations

import unittest

from src.names import from_roman, split_generation, successor_name, to_roman


class RomanNumeralTests(unittest.TestCase):
    def test_round_trips_the_numbers_a_lineage_will_ever_reach(self) -> None:
        for n in range(1, 200):
            self.assertEqual(from_roman(to_roman(n)), n)

    def test_subtractive_forms_are_used(self) -> None:
        self.assertEqual(to_roman(4), "IV")
        self.assertEqual(to_roman(9), "IX")
        self.assertEqual(to_roman(40), "XL")

    def test_non_canonical_numerals_are_rejected(self) -> None:
        # Accepting anything parseable would make an agent named "MIX" or
        # "DID" read as a generation of some other agent.
        for text in ("IIII", "VV", "IC", "", "quasar", "I I"):
            self.assertIsNone(from_roman(text))


class SplitGenerationTests(unittest.TestCase):
    def test_a_plain_name_is_the_first_of_its_line(self) -> None:
        self.assertEqual(split_generation("Quasar"), ("Quasar", 1))
        self.assertEqual(split_generation("swift-otter"), ("swift-otter", 1))

    def test_a_trailing_numeral_is_the_generation(self) -> None:
        self.assertEqual(split_generation("Quasar II"), ("Quasar", 2))
        self.assertEqual(split_generation("Quasar XIV"), ("Quasar", 14))

    def test_a_name_that_is_only_a_numeral_stays_whole(self) -> None:
        self.assertEqual(split_generation("MIX"), ("MIX", 1))
        self.assertEqual(split_generation("IV"), ("IV", 1))


class SuccessorNameTests(unittest.TestCase):
    def test_the_first_successor_is_the_second_generation(self) -> None:
        self.assertEqual(successor_name("Quasar"), "Quasar II")

    def test_each_handoff_advances_one_generation(self) -> None:
        name = "Quasar"
        for expected in ("Quasar II", "Quasar III", "Quasar IV", "Quasar V"):
            name = successor_name(name)
            self.assertEqual(name, expected)

    def test_generations_already_in_the_workspace_are_skipped(self) -> None:
        # The predecessor keeps its name when it retires, and so does every
        # earlier generation, so the offered name has to be a free one.
        self.assertEqual(
            successor_name("Quasar", {"Quasar II", "Quasar III"}), "Quasar IV"
        )

    def test_the_check_ignores_case_the_way_the_workspace_does(self) -> None:
        self.assertEqual(successor_name("Quasar", {"quasar ii"}), "Quasar III")

    def test_a_multi_word_name_keeps_all_of_it(self) -> None:
        self.assertEqual(successor_name("swift otter"), "swift otter II")

    def test_an_empty_predecessor_offers_nothing(self) -> None:
        self.assertEqual(successor_name(""), "")


if __name__ == "__main__":
    unittest.main()
