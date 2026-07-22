from __future__ import annotations

import sqlite3

import pytest

from server.store import SQLiteStore


def test_public_creation_usage_is_an_exact_additive_schema_with_day_index(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-additive.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE legacy_canary (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_canary (value) VALUES ('preserved')")

    SQLiteStore(database_path).close()

    with sqlite3.connect(database_path) as connection:
        columns = connection.execute(
            "PRAGMA table_info(public_creation_usage)"
        ).fetchall()
        assert [(row[1], row[2], row[3], row[4], row[5]) for row in columns] == [
            ("identity_kind", "TEXT", 1, None, 1),
            ("identity_hash", "TEXT", 1, None, 2),
            ("usage_day", "TEXT", 1, None, 3),
            ("amount", "INTEGER", 1, "0", 0),
            ("updated_at", "TEXT", 1, None, 0),
        ]
        assert connection.execute(
            "PRAGMA foreign_key_list(public_creation_usage)"
        ).fetchall() == []
        indexes = connection.execute(
            "PRAGMA index_list(public_creation_usage)"
        ).fetchall()
        assert {row[1] for row in indexes} == {
            "public_creation_usage_day_idx",
            "sqlite_autoindex_public_creation_usage_1",
        }
        assert [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(public_creation_usage_day_idx)"
            ).fetchall()
        ] == ["usage_day"]
        assert connection.execute("SELECT value FROM legacy_canary").fetchone() == (
            "preserved",
        )


def test_public_creation_usage_rejects_same_name_wrong_index_drift(tmp_path) -> None:
    database_path = tmp_path / "creation-index-drift.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE public_creation_usage (
                identity_kind TEXT NOT NULL CHECK (
                    identity_kind IN ('session', 'ip', 'global')
                ),
                identity_hash TEXT NOT NULL,
                usage_day TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity_hash, usage_day)
            );
            CREATE INDEX public_creation_usage_day_idx
            ON public_creation_usage(identity_hash);
            """
        )

    with pytest.raises(RuntimeError, match="public creation usage"):
        SQLiteStore(database_path)


@pytest.mark.parametrize(
    "index_suffix",
    ["usage_day DESC", "usage_day COLLATE NOCASE"],
)
def test_public_creation_usage_rejects_index_order_or_collation_drift(
    tmp_path,
    index_suffix: str,
) -> None:
    database_path = tmp_path / "creation-index-semantics-drift.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE public_creation_usage (
                identity_kind TEXT NOT NULL CHECK (
                    identity_kind IN ('session', 'ip', 'global')
                ),
                identity_hash TEXT NOT NULL,
                usage_day TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity_hash, usage_day)
            );
            CREATE INDEX public_creation_usage_day_idx
            ON public_creation_usage({index_suffix});
            """
        )

    with pytest.raises(RuntimeError, match="public creation usage"):
        SQLiteStore(database_path)


def test_public_creation_usage_missing_usage_day_fails_with_typed_schema_error(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-missing-day.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE public_creation_usage (
                identity_kind TEXT NOT NULL,
                identity_hash TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity_hash)
            )
            """
        )

    with pytest.raises(RuntimeError, match="public creation usage"):
        SQLiteStore(database_path)


def test_public_creation_usage_rejects_missing_constraints_or_foreign_keys(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-schema-drift.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE raw_identity_parent (value TEXT PRIMARY KEY);
            CREATE TABLE public_creation_usage (
                identity_kind TEXT NOT NULL,
                identity_hash TEXT NOT NULL REFERENCES raw_identity_parent(value),
                usage_day TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity_hash, usage_day)
            );
            CREATE INDEX public_creation_usage_day_idx
            ON public_creation_usage(usage_day);
            """
        )

    with pytest.raises(RuntimeError, match="public creation usage"):
        SQLiteStore(database_path)


@pytest.mark.parametrize(
    "table_sql",
    [
        """
        CREATE TABLE public_creation_usage (
            identity_kind TEXT NOT NULL,
            identity_hash TEXT NOT NULL,
            usage_day TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (identity_kind, identity_hash, usage_day)
        )
        """,
        """
        CREATE TABLE public_creation_usage (
            identity_kind TEXT NOT NULL CHECK (
                identity_kind IN ('session', 'ip', 'global')
            ),
            identity_hash TEXT NOT NULL,
            usage_day TEXT NOT NULL,
            amount INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (identity_kind, identity_hash, usage_day)
        )
        """,
    ],
)
def test_public_creation_usage_rejects_each_missing_check_constraint(
    tmp_path,
    table_sql: str,
) -> None:
    database_path = tmp_path / "creation-check-drift.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(table_sql)
        connection.execute(
            """
            CREATE INDEX public_creation_usage_day_idx
            ON public_creation_usage(usage_day)
            """
        )

    with pytest.raises(RuntimeError, match="public creation usage"):
        SQLiteStore(database_path)


def test_public_creation_usage_rejects_an_extra_restrictive_constraint(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-extra-check-drift.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE public_creation_usage (
                identity_kind TEXT NOT NULL CHECK (
                    identity_kind IN ('session', 'ip', 'global')
                ),
                identity_hash TEXT NOT NULL,
                usage_day TEXT NOT NULL CHECK (usage_day = '2099-01-01'),
                amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity_hash, usage_day)
            );
            CREATE INDEX public_creation_usage_day_idx
            ON public_creation_usage(usage_day);
            """
        )

    with pytest.raises(RuntimeError, match="public creation usage"):
        SQLiteStore(database_path)


def test_public_creation_usage_extra_constraint_fails_readiness(tmp_path) -> None:
    database_path = tmp_path / "creation-extra-check-readiness.sqlite3"
    store = SQLiteStore(database_path)
    assert store.is_ready()
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            DROP INDEX public_creation_usage_day_idx;
            ALTER TABLE public_creation_usage
            RENAME TO public_creation_usage_original;
            CREATE TABLE public_creation_usage (
                identity_kind TEXT NOT NULL CHECK (
                    identity_kind IN ('session', 'ip', 'global')
                ),
                identity_hash TEXT NOT NULL,
                usage_day TEXT NOT NULL CHECK (usage_day = '2099-01-01'),
                amount INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity_hash, usage_day)
            );
            DROP TABLE public_creation_usage_original;
            CREATE INDEX public_creation_usage_day_idx
            ON public_creation_usage(usage_day);
            """
        )

    assert store.is_ready() is False


def test_public_creation_usage_rejects_required_checks_hidden_in_comments(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-comment-check-drift.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE public_creation_usage (
                identity_kind TEXT NOT NULL
                    /* CHECK (identity_kind IN ('session', 'ip', 'global')) */,
                identity_hash TEXT NOT NULL,
                usage_day TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0 /* CHECK (amount >= 0) */,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (identity_kind, identity_hash, usage_day)
            );
            CREATE INDEX public_creation_usage_day_idx
            ON public_creation_usage(usage_day);
            """
        )

    with pytest.raises(RuntimeError, match="public creation usage"):
        SQLiteStore(database_path)


def test_public_creation_usage_allows_only_case_and_whitespace_variation(
    tmp_path,
) -> None:
    database_path = tmp_path / "creation-harmless-ddl-variation.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            cReAtE tAbLe public_creation_usage(
                identity_kind tExT nOt nUlL cHeCk(
                    identity_kind iN('session', 'ip', 'global')
                ),
                identity_hash TeXt NoT NuLl,
                usage_day TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(identity_kind,identity_hash,usage_day)
            );
            CrEaTe InDeX public_creation_usage_day_idx
            oN public_creation_usage ( usage_day );
            """
        )

    store = SQLiteStore(database_path)
    assert store.is_ready()


def test_public_creation_usage_constraints_reject_invalid_direct_rows(tmp_path) -> None:
    database_path = tmp_path / "creation-direct-constraints.sqlite3"
    SQLiteStore(database_path).close()

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO public_creation_usage (
                    identity_kind, identity_hash, usage_day, amount, updated_at
                ) VALUES ('raw', 'raw', '2026-07-21', 1, 'now')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO public_creation_usage (
                    identity_kind, identity_hash, usage_day, amount, updated_at
                ) VALUES ('session', 'hash', '2026-07-21', -1, 'now')
                """
            )


def test_additive_creation_ledger_does_not_backfill_live_usage(tmp_path) -> None:
    database_path = tmp_path / "creation-no-backfill.sqlite3"
    store = SQLiteStore(database_path)
    store.close()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO usage_ledger (
                identity_kind, identity_hash, usage_day, amount,
                last_admitted_at, updated_at
            ) VALUES ('global', 'public-live-global', '2026-07-21', 7, NULL, 'now')
            """
        )
        connection.execute("DROP TABLE public_creation_usage")

    SQLiteStore(database_path).close()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM public_creation_usage"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT amount FROM usage_ledger WHERE identity_kind = 'global'"
        ).fetchone() == (7,)
