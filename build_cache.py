
from local_cache import LocalCardDatabase

def main():
    db = LocalCardDatabase()
    print()
    print("=== PokeRarity Local Card Database ===")
    print(f"Current cache: {db.count():,} cards")
    print()
    try:
        try:
            revision = db.get_remote_revision()
        except Exception:
            revision = None
        db.rebuild_from_repo(lambda msg: print(msg, flush=True), source_revision=revision)
        print()
        print("Local database update complete.")
    except Exception as e:
        print()
        print("Could not download the full GitHub snapshot:")
        print(e)
        print()
        print("PokeRarity will still run with the bundled seed cache.")
        print("You can retry later with Update Local DB inside the app.")

if __name__ == "__main__":
    main()
