"""The ground-truth comparison must not penalise presentation.

The pairs below are shortened from replacements the corpus run scored as
disagreements while the text was the same: the amending act and the consolidation
simply print it differently.
"""

from a2t.corpus.run import agrees, comparable


def test_a_paragraph_number_and_note_markers_are_not_a_difference():
    new = ("2. Member States shall clearly define any technical specifications "
           "referred to in Article 4(1) of Directive 2014/24/EU (*).")
    published = ("Member States shall clearly define any technical specifications "
                 "referred to in Article 4(1) of Directive 2014/24/EU ( ).")
    assert agrees(new, [published])


def test_the_text_of_a_footnote_following_the_provision_is_left_out():
    new = ("The Commission shall report (*) every year. "
           "(*) Directive 2012/27/EU of the European Parliament and of the Council "
           "of 25 October 2012 on energy efficiency (OJ L 315, 14.11.2012, p. 1).")
    assert comparable(new) == "The Commission shall report every year."


def test_spacing_in_a_formula_and_hyphen_variants_are_not_a_difference():
    assert agrees("emissions of CO2, and third‐country firms",
                  ["emissions of CO 2 , and third-country firms"])


def test_a_replaced_article_may_carry_the_heading_the_consolidation_keeps_apart():
    new = ("Article 28c Provisions for monitoring, reporting and verification "
           "1. Operators shall monitor emissions. 2. They shall report them.")
    published = "Operators shall monitor emissions. They shall report them."
    assert agrees(new, [published])


def test_inner_point_markers_are_not_a_difference():
    new = "used: (a) in solid fuels; (b) in liquid fuels."
    assert agrees(new, ["used: in solid fuels; in liquid fuels."])


def test_a_replacement_that_became_several_provisions_matches_the_run():
    """The first subparagraph replaced by two: published as two siblings."""
    new = "Member States shall adopt rules. They shall notify the Commission."
    assert not agrees(new, ["Member States shall adopt rules."])
    assert agrees(new, ["Member States shall adopt rules.",
                        "Member States shall adopt rules.\nThey shall notify the Commission."])


def test_a_different_wording_still_disagrees():
    assert not agrees("Operators shall report every year.",
                      ["Operators shall report every second year."])
    assert not agrees("When reporting the information referred to in paragraphs 1 and 2",
                      ["Protected undertakings shall have the right to decline"])
