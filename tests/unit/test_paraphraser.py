from src.content.paraphraser import RuleBasedParaphraser


def test_paraphraser_preserves_links():
    p = RuleBasedParaphraser(synonyms={"try": ["attempt"]}, seed=1)
    text = "I will try this: https://example.com/test"
    res = p.paraphrase(text, intensity=1.0)
    assert "https://example.com/test" in res.paraphrased
    assert "attempt" in res.paraphrased.lower()
