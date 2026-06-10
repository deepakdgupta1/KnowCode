from knowcode.indexing.scanner import Scanner

def test_supported_extensions_contains_jsx_and_tsx():
    # Ensure the scanner reports .jsx and .tsx as supported extensions
    assert ".jsx" in Scanner.SUPPORTED_EXTENSIONS, "Scanner should support .jsx files"
    assert ".tsx" in Scanner.SUPPORTED_EXTENSIONS, "Scanner should support .tsx files"
