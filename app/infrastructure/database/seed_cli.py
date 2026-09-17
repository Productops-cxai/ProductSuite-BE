"""Run: python -m app.infrastructure.database.seed_cli"""

from app.infrastructure.database.seeder import run_seeder


if __name__ == "__main__":
    run_seeder()
    print("Schema sync + seed completed.")
