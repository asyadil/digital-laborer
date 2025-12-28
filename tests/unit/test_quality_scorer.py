from src.content.quality_scorer import QualityScorer


def test_quality_score_in_range():
    scorer = QualityScorer(min_length=10, max_length=50)
    content = "This is a short but valid sentence. " * 5
    qa = scorer.assess(content)
    assert 0.0 <= qa.score <= 1.0
    assert set(qa.breakdown.keys()) == {"length", "spam", "flow", "link_placement", "readability"}
