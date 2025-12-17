"""Temporal analysis of git history."""

from datetime import datetime, timezone
from pathlib import Path

from git import Repo

from knowcode.models import (
    Entity,
    EntityKind,
    Location,
    ParseResult,
    Relationship,
    RelationshipKind,
)


class TemporalAnalyzer:
    """Analyzes git history to build temporal graph."""

    def __init__(self, root_dir: str | Path) -> None:
        """Initialize temporal analyzer.
        
        Args:
            root_dir: Root directory of the git repo.
        """
        self.root_dir = Path(root_dir).resolve()
        try:
            self.repo = Repo(self.root_dir)
        except Exception:
            # Not a git repo or git not installed
            self.repo = None

    def analyze_history(self, limit: int = 100) -> ParseResult:
        """Analyze git commit history.

        Args:
            limit: Maximum number of commits to analyze.

        Returns:
            ParseResult containing COMMIT and AUTHOR entities and relationships.
        """
        if not self.repo:
            return ParseResult(
                file_path="git-history",
                entities=[],
                relationships=[],
                errors=["Not a valid git repository"],
            )

        entities: list[Entity] = []
        relationships: list[Relationship] = []
        errors: list[str] = []

        try:
            # Iterating commits from HEAD
            commits = list(self.repo.iter_commits("HEAD", max_count=limit))
            
            for commit in commits:
                commit_hash = commit.hexsha
                short_hash = commit_hash[:7]
                commit_id = f"commit::{commit_hash}"
                
                # Author Entity
                author_name = commit.author.name
                author_email = commit.author.email
                author_id = f"author::{author_email}"
                
                # Create Author entity if not exists (we rely on graph builder to dedupe)
                author_entity = Entity(
                    id=author_id,
                    kind=EntityKind.AUTHOR,
                    name=author_name,
                    qualified_name=author_email,
                    location=Location("git", 0, 0),
                    metadata={"email": author_email}
                )
                entities.append(author_entity)

                # Create Commit Entity
                # Use commit message as description/docstring
                committed_date = datetime.fromtimestamp(commit.committed_date, tz=timezone.utc)
                commit_entity = Entity(
                    id=commit_id,
                    kind=EntityKind.COMMIT,
                    name=short_hash,
                    qualified_name=commit_hash,
                    location=Location("git", 0, 0),
                    docstring=commit.message.strip(),
                    metadata={
                        "date": committed_date.isoformat(),
                        "timestamp": str(commit.committed_date)
                    }
                )
                entities.append(commit_entity)

                # Relationship: AUTHOR -> AUTHORED -> COMMIT
                relationships.append(
                    Relationship(
                        source_id=author_id,
                        target_id=commit_id,
                        kind=RelationshipKind.AUTHORED
                    )
                )

                # Relationship: COMMIT -> MODIFIED -> FILE (Module)
                # We need to find what files changed.
                # commit.stats.files gives us list of changed files
                for file_path, stats in commit.stats.files.items():
                    # stats is dict like {'insertions': 1, 'deletions': 0, 'lines': 1}
                    # file_path is relative to repo root
                    
                    # Construct module ID for the file
                    # We assume standard module ID format: /abs/path/to/file::filename
                    # But we only have relative path here.
                    # We need to reconstruct the absolute path ID used by other parsers.
                    abs_path = self.root_dir / file_path
                    module_name = Path(file_path).stem
                    target_module_id = f"{abs_path}::{module_name}"
                    
                    relationships.append(
                        Relationship(
                            source_id=commit_id,
                            target_id=target_module_id,
                            kind=RelationshipKind.MODIFIED,
                            metadata={
                                "insertions": str(stats.get("insertions", 0)),
                                "deletions": str(stats.get("deletions", 0))
                            }
                        )
                    )
                    
                    # Also Relationship: MODULE -> CHANGED_BY -> COMMIT
                    relationships.append(
                        Relationship(
                            source_id=target_module_id,
                            target_id=commit_id,
                            kind=RelationshipKind.CHANGED_BY
                        )
                    )

        except Exception as e:
            errors.append(f"Error analyzing git history: {e}")

        return ParseResult(
            file_path="git-history",
            entities=entities,
            relationships=relationships,
            errors=errors,
        )
