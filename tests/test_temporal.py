"""Tests for Temporal Integration."""

import shutil
from pathlib import Path
import pytest
from git import Repo, Actor

from knowcode.temporal import TemporalAnalyzer
from knowcode.models import EntityKind, RelationshipKind

@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repo with history."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    
    repo = Repo.init(repo_dir)
    author = Actor("Test User", "test@example.com")
    committer = Actor("Test User", "test@example.com")
    
    # Commit 1: Add file
    file1 = repo_dir / "file1.py"
    file1.write_text("print('hello')", encoding="utf-8")
    repo.index.add([str(file1)])
    repo.index.commit("Initial commit", author=author, committer=committer)
    
    # Commit 2: Modify file
    file1.write_text("print('hello world')", encoding="utf-8")
    repo.index.add([str(file1)])
    repo.index.commit("Update file1", author=author, committer=committer)
    
    return repo_dir

def test_temporal_analysis(git_repo):
    """Test standard temporal analysis."""
    analyzer = TemporalAnalyzer(git_repo)
    result = analyzer.analyze_history()
    
    assert not result.errors
    
    # Valid Commits
    commits = [e for e in result.entities if e.kind == EntityKind.COMMIT]
    assert len(commits) == 2
    
    # Validate Author
    authors = {e.id for e in result.entities if e.kind == EntityKind.AUTHOR}
    assert len(authors) == 1
    # Check name on one of them
    author_ent = next(e for e in result.entities if e.kind == EntityKind.AUTHOR)
    assert author_ent.name == "Test User"
    
    # Validate Relationships
    # Author -> Authored -> Commit
    authored_rels = [r for r in result.relationships if r.kind == RelationshipKind.AUTHORED]
    assert len(authored_rels) == 2 # 2 commits by same author
    
    # Commit -> Modified -> File
    # We need to check if proper IDs are generated. 
    # file1.py should be targeted.
    modified_rels = [r for r in result.relationships if r.kind == RelationshipKind.MODIFIED]
    assert len(modified_rels) >= 1
    
    # Check if target ID looks right (should be absolute path)
    target_id = modified_rels[-1].target_id # Latest modification
    assert "file1.py" in target_id
    assert "::file1" in target_id
    
    # CHANGED_BY (Entity -> Commit)
    changed_by_rels = [r for r in result.relationships if r.kind == RelationshipKind.CHANGED_BY]
    assert len(changed_by_rels) >= 1
    assert changed_by_rels[0].source_id == target_id
