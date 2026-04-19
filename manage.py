"""Controller management commands. Run from second-spring-control folder:

    python manage.py force-advance           # push all edited items to ready_to_publish
    python manage.py clear-errors            # wipe error fields
    python manage.py diagnose                # show gate reasons for every edited item
    python manage.py stages                  # print current stage counts
    python manage.py reset-stuck             # move stuck drafting items back to skeleton

No PowerShell quoting nightmares.
"""
from __future__ import annotations

import sys

from control import db, pipeline
from control.quality import run_gate


def cmd_force_advance():
    rows = db.fetch_all("SELECT id FROM items WHERE stage = 'edited'")
    moved = 0
    for r in rows:
        a = pipeline.transition(r['id'], 'reviewing', actor='user', note='manage force', skip_gate=True)
        b = pipeline.transition(r['id'], 'ready_to_publish', actor='user', note='manage force', skip_gate=True)
        if a.ok and b.ok:
            moved += 1
            db.execute("UPDATE items SET error = NULL WHERE id = ?", (r['id'],))
    print(f"force-advanced {moved}/{len(rows)} items to ready_to_publish")


def cmd_clear_errors():
    db.execute("UPDATE items SET error = NULL")
    print("all error fields cleared")


def cmd_diagnose():
    rows = db.fetch_all("SELECT id, title, stage FROM items WHERE stage = 'edited'")
    if not rows:
        print("no items in 'edited' stage")
        return
    for r in rows:
        result = run_gate(r['id'], 'edited')
        flag = "PASS" if result.passed else "FAIL"
        print(f"[{flag}] id={r['id']:<4} reason={result.reason!r}")
        print(f"        title: {r['title'][:70]}")


def cmd_stages():
    counts = pipeline.stage_counts()
    for stage, n in counts.items():
        print(f"  {stage:<20} {n}")


def cmd_reset_stuck():
    """drafting items that never finished need to go back to skeleton."""
    db.execute("UPDATE items SET stage = 'skeleton' WHERE stage = 'drafting'")
    print("drafting items reset to skeleton")


def cmd_preview():
    """Write all ready_to_publish items as .md files to the Astro content
    folder WITHOUT any git activity. Lets you preview the site locally via
    `npm run dev` before touching GitHub or the VPS.
    """
    from pathlib import Path
    from control import publish
    from control.config import settings

    rows = db.fetch_all(
        "SELECT * FROM items WHERE stage = 'ready_to_publish'"
    )
    if not rows:
        print("no items at ready_to_publish stage")
        return

    content_dir = Path(settings.astro_content_dir)
    if not content_dir.exists():
        print(f"ERROR: Astro content dir not found at {content_dir}")
        print("Check ASTRO_CONTENT_DIR in .env")
        return

    written = 0
    for r in rows:
        item = dict(r)
        try:
            target = publish.write_content_file(item)
            print(f"  wrote {target.name}")
            written += 1
        except Exception as e:
            print(f"  FAILED id={r['id']}: {e}")

    print()
    print(f"wrote {written}/{len(rows)} .md files to {content_dir}")
    print()
    print("Now preview the site locally:")
    print(f"  cd {settings.astro_repo_dir}")
    print("  npm install    (first time only)")
    print("  npm run dev")
    print()
    print("Then open http://localhost:4321 in your browser.")


COMMANDS = {
    "force-advance": cmd_force_advance,
    "clear-errors":  cmd_clear_errors,
    "diagnose":      cmd_diagnose,
    "stages":        cmd_stages,
    "reset-stuck":   cmd_reset_stuck,
    "preview":       cmd_preview,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print("usage: python manage.py <command>")
        print("commands:")
        for name in COMMANDS:
            print(f"  {name}")
        sys.exit(1)
    COMMANDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
