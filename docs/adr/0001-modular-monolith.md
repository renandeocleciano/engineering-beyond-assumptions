# ADR 0001: Start with a single Python service

Status: Accepted

Use FastAPI, SQLAlchemy 2, Alembic and PostgreSQL in one application. Keep experiments in the same repository and run them separately from request handling. This keeps the setup and operational model small while allowing real database behavior to be studied. It does not prescribe a distributed architecture; revisit the decision when measured requirements justify additional components.
