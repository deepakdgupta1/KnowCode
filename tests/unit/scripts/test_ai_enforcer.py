import os
import ast
import tempfile
from unittest.mock import patch, MagicMock

# Assuming ai_enforcer is importable, we may need to modify sys.path
import sys

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
)
from scripts import ai_enforcer


def test_generate_docstring_success():
    """Test that generate_docstring correctly formats the LLM response."""
    with patch("urllib.request.urlopen") as mock_urlopen:
        # Mock the HTTP response from the LiteLLM Proxy
        mock_response = MagicMock()
        mock_response.read.return_value = (
            b'{"choices": [{"message": {"content": "Test docstring."}}]}'
        )
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        docstring = ai_enforcer.generate_docstring(
            signature="def test():", body_preview="pass", api_key="fake-key"
        )
        assert docstring == "Test docstring."


def test_apply_docstrings_inserts_correctly():
    """Test that missing docstrings are properly identified and inserted."""
    code_with_no_docstring = "def missing_doc(a, b):\n    print(a)\n    return a + b\n"

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code_with_no_docstring)
        temp_path = f.name

    try:
        # Mock the LLM generation so we don't hit the network
        with patch("scripts.ai_enforcer.generate_docstring") as mock_gen:
            mock_gen.return_value = "Mocked docstring."

            result = ai_enforcer.apply_docstrings(temp_path, "fake-key")

            assert result is True
            mock_gen.assert_called_once()

            with open(temp_path, "r") as f:
                modified_code = f.read()

            # Verify the docstring was inserted with proper indentation
            assert '    """Mocked docstring."""' in modified_code

            # Verify it parses correctly
            tree = ast.parse(modified_code)
            assert isinstance(tree.body[0], ast.FunctionDef)
            assert ast.get_docstring(tree.body[0]) == "Mocked docstring."
    finally:
        os.remove(temp_path)


def test_apply_docstrings_skips_existing():
    """Test that existing docstrings are not overwritten."""
    code_with_docstring = 'def has_doc():\n    """Existing docstring."""\n    pass\n'

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(code_with_docstring)
        temp_path = f.name

    try:
        with patch("scripts.ai_enforcer.generate_docstring") as mock_gen:
            result = ai_enforcer.apply_docstrings(temp_path, "fake-key")

            # Should return False as no changes were made
            assert result is False
            mock_gen.assert_not_called()
    finally:
        os.remove(temp_path)
