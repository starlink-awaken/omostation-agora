

def test_docno_not_classified_as_handwriting():
    """发文字号(京卫医〔2026〕45号)是版头印刷体, 低置信也不应归手写签批(全链路实测)。"""
    from agora.server.tools_bos.ocr import TextBox, classify_handwriting

    boxes = [TextBox(x=480, y=270, w=290, h=38, text="京卫医〔2026〕45号", confidence=0.3)]
    out, members = classify_handwriting(boxes)
    assert not out and not members
