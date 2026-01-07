import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

def get_git_log(range_str="HEAD~1..HEAD"):
    """Get raw git log for the specified range."""
    # Format: hash|author|date|message matches the "structured" requirement loosely
    # But user wants to follow EVOLUTION.md template.
    # We will try to parse standard conventional commits if available.
    try:
        cmd = ["git", "log", range_str, "--pretty=format:%h|%an|%ad|%s%n%b", "--date=short"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        output = result.stdout.strip()
        if not output:
            return []
        # Split by double newline to separate commits
        return output.split("\n\n")
    except subprocess.CalledProcessError:
        print(f"Error reading git log for range {range_str}")
        return []

def get_latest_tag():
    """Return the most recent git tag reachable from HEAD."""
    try:
        cmd = ["git", "describe", "--tags", "--abbrev=0"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        tag = result.stdout.strip()
        return tag or None
    except subprocess.CalledProcessError:
        return None

def compute_confidence(commits, known_types):
    """Compute confidence based on conventional commit parsing coverage."""
    if not commits:
        return "low"
    known = sum(1 for c in commits if c["type"].split("(")[0] in known_types)
    ratio = known / len(commits)
    if ratio == 1.0:
        return "high"
    if ratio >= 0.6:
        return "medium"
    return "low"

def parse_commit(commit_text):
    """Parse a single commit block."""
    lines = commit_text.strip().split('\n')
    header = lines[0].split('|')
    if len(header) < 4:
        return None
    
    commit_hash = header[0]
    author = header[1]
    date = header[2]
    subject = header[3]
    body = "\n".join(lines[1:]) if len(lines) > 1 else ""
    
    # Simple conventional commit parsing
    type_scope = "other"
    desc = subject
    if ':' in subject:
        parts = subject.split(':', 1)
        type_scope = parts[0].strip()
        desc = parts[1].strip()
    
    return {
        "hash": commit_hash,
        "author": author,
        "date": date,
        "subject": subject,
        "body": body,
        "type": type_scope,
        "desc": desc
    }

def generate_entry(commits, range_str, origin="generated", summary=None):
    if not commits:
        return ""

    # Group by type
    groups = {}
    for c in commits:
        t = c['type'].lower()
        if '(' in t: # handle feat(ui)
            t = t.split('(')[0]
        if t not in groups:
            groups[t] = []
        groups[t].append(c)

    # Markdown Generation strictly following a simplified version of EVOLUTION.md structure
    # We can't auto-generate "Temporal Context" or "Architectural Impact" easily without AI.
    # We will put placeholders.
    
    now = datetime.now().strftime("%Y-%m-%d")
    known_types = {"feat", "fix", "refactor", "perf", "test", "docs", "chore"}
    confidence = compute_confidence(commits, known_types)
    output = []
    output.append(f"## [Unreleased] - {now}")
    output.append("")
    output.append(f"**Origin:** {origin.title()}")
    output.append(f"**Range:** `{range_str}`")
    output.append(f"**Commit Count:** {len(commits)}")
    output.append(f"**Confidence:** {confidence}")
    output.append("")
    # Determine intent from the most common type or the first commit
    focus = "Routine Maintenance"
    if 'feat' in groups:
        focus = "Feature Development"
    if 'fix' in groups:
        focus = "Bug Fixes"
    
    output.append(f"**Focus:** {focus}")
    output.append("")
    output.append("### 🧠 Temporal Context & Intent")
    if summary:
        output.append(f"> {summary}")
    else:
        output.append("> *Auto-generated: Add context about why these changes were made.*")
    output.append("")
    output.append("### 🏗️ Architectural Impact")
    output.append("> *Auto-generated: Describe high-level architectural shifts.*")
    output.append("")
    output.append("### 📝 Delta Changes")
    output.append("")

    # Map conventional types to EVOLUTION.md sections
    type_map = {
        'feat': '🚀 Features',
        'fix': '🐛 Fixes',
        'refactor': '🔨 Refactoring',
        'perf': '⚡ Performance',
        'test': '✅ Testing',
        'docs': '📚 Documentation',
        'chore': '🔧 Maintenance'
    }

    # Features
    for type_key in ['feat', 'fix', 'chore', 'docs', 'refactor', 'perf', 'test']:
        if type_key in groups:
            header = type_map.get(type_key, f"Changes ({type_key})")
            output.append(f"#### {header}")
            for c in groups[type_key]:
                # Attempt to extract scope
                scope = ""
                s_subj = c['subject']
                if ':' in s_subj:
                    pre = s_subj.split(':')[0]
                    if '(' in pre and ')' in pre:
                        scope = pre.split('(')[1].split(')')[0]
                
                prefix = f"**{scope}:** " if scope else ""
                output.append(f"* {prefix}{c['desc']} (`{c['hash']}`)")
                if c['body']:
                    # Indent body as context
                    for line in c['body'].split('\n'):
                        if line.strip():
                            output.append(f"    * *Context:* {line.strip()}")
            output.append("")
            
    # Handle others
    others = [c for c in commits if c['type'].split('(')[0] not in type_map]
    if others:
        output.append("#### 📦 Other Changes")
        for c in others:
            output.append(f"* {c['subject']} (`{c['hash']}`)")
        output.append("")

    return "\n".join(output)

def main():
    changelog_path = Path("CHANGELOG.md")

    parser = argparse.ArgumentParser(description="Generate a changelog entry from git history.")
    parser.add_argument(
        "--range",
        dest="range_str",
        default=None,
        help="Git commit range (e.g., v2.1.0..HEAD).",
    )
    parser.add_argument(
        "--use-last-tag",
        action="store_true",
        help="Use the latest tag as the start of the range (falls back if none).",
    )
    parser.add_argument(
        "--origin",
        default="generated",
        help="Origin label for the entry (generated or curated).",
    )
    parser.add_argument(
        "--summary",
        default=None,
        help="Plain-English summary of the changes to include in the entry.",
    )
    parser.add_argument(
        "--summary-file",
        default=None,
        help="Path to a file containing the plain-English summary.",
    )
    args = parser.parse_args()

    if args.range_str:
        range_str = args.range_str
    elif args.use_last_tag:
        tag = get_latest_tag()
        range_str = f"{tag}..HEAD" if tag else "HEAD~5..HEAD"
    else:
        range_str = "HEAD~5..HEAD"

    # In a real CI, we might compare against the last tag.
    commits_raw = get_git_log(range_str)
    parsed_commits = [parse_commit(c) for c in commits_raw if c]
    parsed_commits = [c for c in parsed_commits if c] # filter Nones

    summary = args.summary
    if args.summary_file:
        try:
            summary = Path(args.summary_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"Failed to read summary file: {exc}")
            sys.exit(1)

    new_entry = generate_entry(parsed_commits, range_str, origin=args.origin, summary=summary)
    
    if not new_entry:
        print("No commits found to generate changelog.")
        return

    print("Generated Changelog Entry:")
    print(new_entry)

    # Append mode (or prepend? Usually changelogs are prepended, but user asked for Append Mode)
    # "write them (append mode) to a change log markdown file"
    # I will stick to append as requested, although it's unusual for changelogs.
    
    mode = 'a' if changelog_path.exists() else 'w'
    with open(changelog_path, mode, encoding='utf-8') as f:
        if mode == 'a':
            f.write("\n\n---\n\n")
        f.write(new_entry)
    
    print(f"\nSuccessfully appended to {changelog_path}")

if __name__ == "__main__":
    main()
