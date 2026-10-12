import pytest
from scoring import score_moment, segment_words

def test_perfect_score():
    signals={k:100 for k in ("hook","emotion","story","novelty","quotability","opinion","call_to_action")}
    assert score_moment(signals)["score"]==100

def test_invalid_signal_rejected():
    with pytest.raises(ValueError): score_moment({"hook":101})

def test_segments_obey_bounds():
    words=[{"text":"sentence." if i%8==7 else "word","start":i*2,"end":i*2+1} for i in range(80)]
    result=segment_words(words,15,30)
    assert result and all(15<=x["end"]-x["start"]<=30 for x in result)

def test_bad_or_empty_words():
    assert segment_words([])==[]
    assert segment_words([{"text":"bad","start":-1,"end":0}])==[]
