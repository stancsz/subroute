"""Transactional workboard. Unknown execution is quarantined, never retried."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import sqlite3
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class GoalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=4000)
    acceptance: str = Field(min_length=1, max_length=4000)
    authority: str = Field(min_length=1, max_length=4000)
    max_tasks: int = Field(default=30, ge=1, le=200)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str
    node: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    workspace: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    instruction: str = Field(min_length=1, max_length=8000)
    kind: Literal["work", "review"] = "work"
    reviews_task: str | None = None
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    max_turns: int = Field(default=16, ge=1, le=80)
    max_attempts: int = Field(default=2, ge=1, le=3)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: str | Path, lease_seconds: int = 45, clock=time.time):
        self.path = str(path)
        self.lease_seconds = lease_seconds
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    kind TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL);
            """)

    @contextmanager
    def tx(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _get(self, db, table, ident):
        row = db.execute(f"SELECT data FROM {table} WHERE id=?", (ident,)).fetchone()
        if not row:
            raise Conflict(f"Unknown {table}: {ident}")
        return json.loads(row[0])

    def _all(self, db, table):
        return [json.loads(row[0]) for row in db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def _put(self, db, table, data):
        db.execute(f"INSERT INTO {table}(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                   (data["id"], json.dumps(data)))

    def _event(self, db, kind, subject, detail=""):
        db.execute("INSERT INTO events(at,kind,subject,detail) VALUES(?,?,?,?)",
                   (self.clock(), kind, subject, detail[:4000]))

    def _sweep(self, db):
        now = self.clock()
        for task in self._all(db, "tasks"):
            if task["status"] in {"running", "cancelling"} and (
                now >= task["lease_until"] or now >= task["deadline"]
            ):
                task.update(status="reconcile", token=None, reason="Lease/deadline expired; execution outcome unknown")
                self._put(db, "tasks", task)
                self._event(db, "reconciliation_required", task["id"], task["reason"])

    def create_goal(self, spec: GoalSpec):
        with self.tx() as db:
            goal = dict(spec.model_dump(), id=uuid4().hex, status="active", created=self.clock())
            self._put(db, "goals", goal)
            self._event(db, "goal_created", goal["id"])
            return goal

    def create_task(self, spec: TaskSpec):
        with self.tx() as db:
            goal = self._get(db, "goals", spec.goal_id)
            if goal["status"] != "active":
                raise Conflict("Goal must be active")
            tasks = self._all(db, "tasks")
            if sum(t["goal_id"] == spec.goal_id for t in tasks) >= goal["max_tasks"]:
                raise Conflict("Goal task budget exhausted")
            reviewed_attempt = None
            if spec.kind == "review":
                original = self._get(db, "tasks", spec.reviews_task)
                if original["goal_id"] != spec.goal_id or original["status"] != "review" or original["kind"] != "work":
                    raise Conflict("Review must target a work task awaiting review in the same goal")
                if spec.node != original["node"] or spec.workspace != original["workspace"]:
                    raise Conflict("Reviewer must inspect the same machine and workspace")
                reviewed_attempt = original["attempt"]
            elif spec.reviews_task:
                raise Conflict("Only review tasks can name reviews_task")
            task = dict(spec.model_dump(), id=uuid4().hex, status="queued", attempt=0,
                        reviews_attempt=reviewed_attempt, token=None, instance=None, result=None, reason="", created=self.clock())
            self._put(db, "tasks", task)
            self._event(db, "task_created", task["id"])
            return self._public(task)

    @staticmethod
    def _public(task):
        return {k: v for k, v in task.items() if k != "token"}

    def snapshot(self):
        with self.tx() as db:
            self._sweep(db)
            nodes = self._all(db, "nodes")
            for node in nodes:
                node["online"] = self.clock() - node["last_seen"] < self.lease_seconds
            return dict(goals=self._all(db, "goals"), tasks=[self._public(t) for t in self._all(db, "tasks")],
                        nodes=nodes, revision=db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0])

    def events(self, after: int = 0, limit: int = 50):
        with self.tx() as db:
            self._sweep(db)
            return [dict(zip(("seq", "at", "kind", "subject", "detail"), row)) for row in db.execute(
                "SELECT seq,at,kind,subject,detail FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                (max(0, after), min(100, max(1, limit))))]

    def task_get(self, ident):
        with self.tx() as db:
            self._sweep(db)
            task = self._public(self._get(db, "tasks", ident))
            return dict(task, goal=self._get(db, "goals", task["goal_id"]))

    def node_heartbeat(self, node, instance, workspaces):
        with self.tx() as db:
            self._sweep(db)
            row = db.execute("SELECT data FROM nodes WHERE id=?", (node,)).fetchone()
            old = json.loads(row[0]) if row else None
            if old and old["instance"] != instance and self.clock() - old["last_seen"] < self.lease_seconds:
                raise Conflict("Another live runner owns this node identity")
            self._put(db, "nodes", dict(id=node, instance=instance, workspaces=sorted(set(workspaces)), last_seen=self.clock()))
            return {"lease_seconds": self.lease_seconds}

    def claim(self, node, instance, available):
        with self.tx() as db:
            self._sweep(db)
            machine = self._get(db, "nodes", node)
            if machine["instance"] != instance or self.clock() - machine["last_seen"] >= self.lease_seconds:
                raise Conflict("Runner must heartbeat with its current identity first")
            tasks = self._all(db, "tasks")
            busy = {t["workspace"] for t in tasks if t["node"] == node and t["status"] in {"running", "cancelling", "reconcile"}}
            reserved = {t["workspace"]: t["id"] for t in tasks
                        if t["node"] == node and t["kind"] == "work" and t["status"] == "review"}
            for task in tasks:
                if task["node"] != node or task["workspace"] not in set(available) & set(machine["workspaces"]) - busy:
                    continue
                reserved_task = reserved.get(task["workspace"])
                if reserved_task and (task["kind"] != "review" or task["reviews_task"] != reserved_task):
                    continue
                if task["status"] != "queued" or self._get(db, "goals", task["goal_id"])["status"] != "active":
                    continue
                task.update(status="running", attempt=task["attempt"] + 1, token=secrets.token_urlsafe(32),
                            instance=instance, started=self.clock(), lease_until=self.clock() + self.lease_seconds,
                            deadline=self.clock() + task["timeout_seconds"], activity="claimed")
                self._put(db, "tasks", task)
                self._event(db, "task_claimed", task["id"], f"attempt {task['attempt']}")
                return dict(task, goal=self._get(db, "goals", task["goal_id"]))
            return None

    def _owned(self, db, ident, node, instance, attempt, token):
        task = self._get(db, "tasks", ident)
        machine = self._get(db, "nodes", node)
        if (task["node"] != node or task["instance"] != instance or task["attempt"] != attempt
                or machine["instance"] != instance or not task.get("token") or not secrets.compare_digest(task["token"], token)):
            raise Conflict("Stale or foreign attempt")
        return task

    def pulse(self, ident, node, instance, attempt, token, activity=""):
        with self.tx() as db:
            self._sweep(db)
            task = self._owned(db, ident, node, instance, attempt, token)
            if task["status"] not in {"running", "cancelling"}:
                raise Conflict("Attempt is no longer running")
            # A pause stops current execution as well as new claims.
            stop = task["status"] == "cancelling" or self._get(db, "goals", task["goal_id"])["status"] != "active"
            task.update(lease_until=min(self.clock() + self.lease_seconds, task["deadline"]), activity=activity[:200])
            self._put(db, "tasks", task)
            machine = self._get(db, "nodes", node)
            machine["last_seen"] = self.clock()
            self._put(db, "nodes", machine)
            return {"stop": stop, "deadline": task["deadline"]}

    def report(self, ident, node, instance, attempt, token, outcome, result):
        if outcome not in {"success", "failed", "cancelled"} or len(result) > 16000:
            raise Conflict("Invalid terminal report")
        with self.tx() as db:
            self._sweep(db)
            task = self._owned(db, ident, node, instance, attempt, token)
            # Idempotent delivery after a lost acknowledgement.
            if task["status"] in {"review", "failed", "cancelled"}:
                if task.get("outcome") == outcome and task.get("result") == result:
                    return self._public(task)
                raise Conflict("Terminal report already recorded")
            if task["status"] not in {"running", "cancelling"}:
                raise Conflict("Attempt is no longer running")
            if task["status"] == "cancelling" and outcome != "cancelled":
                raise Conflict("Cancelled attempt cannot submit success or failure")
            task.update(status={"success": "review", "failed": "failed", "cancelled": "cancelled"}[outcome],
                        result=result, outcome=outcome, finished=self.clock())
            self._put(db, "tasks", task)
            self._event(db, "task_reported", ident, outcome)
            return self._public(task)

    def cancel(self, ident):
        with self.tx() as db:
            self._sweep(db)
            task = self._get(db, "tasks", ident)
            if task["status"] == "running":
                task["status"] = "cancelling"
            elif task["status"] in {"queued", "failed", "review"}:
                task.update(status="cancelled", token=None)
            elif task["status"] not in {"cancelling", "cancelled", "reconcile"}:
                raise Conflict("Task cannot be cancelled")
            self._put(db, "tasks", task)
            self._event(db, "task_cancelled", ident)
            return self._public(task)

    def reconcile(self, ident, evidence):
        if len(evidence.strip()) < 10:
            raise Conflict("Record evidence that the old process tree has stopped and workspace was inspected")
        with self.tx() as db:
            self._sweep(db)
            task = self._get(db, "tasks", ident)
            if task["status"] != "reconcile":
                raise Conflict("Task is not awaiting reconciliation")
            task.update(status="failed", token=None, reason=evidence[:4000])
            self._put(db, "tasks", task)
            self._event(db, "task_reconciled", ident, evidence)
            return self._public(task)

    def retry(self, ident):
        with self.tx() as db:
            task = self._get(db, "tasks", ident)
            if task["status"] not in {"failed", "cancelled"} or task["attempt"] >= task["max_attempts"]:
                raise Conflict("Task not retryable or attempt budget exhausted")
            if self._get(db, "goals", task["goal_id"])["status"] != "active":
                raise Conflict("Goal must be active")
            task.update(status="queued", token=None, result=None, outcome=None)
            self._put(db, "tasks", task)
            self._event(db, "task_retried", ident)
            return self._public(task)

    def accept(self, ident, evidence, review_task=None):
        if len(evidence.strip()) < 10:
            raise Conflict("Acceptance requires concrete verification evidence")
        with self.tx() as db:
            task = self._get(db, "tasks", ident)
            if task["status"] != "review":
                raise Conflict("Only reported results can be accepted")
            if task["kind"] == "work":
                review = self._get(db, "tasks", review_task)
                if (review["kind"] != "review" or review["reviews_task"] != ident
                        or review["status"] != "accepted" or review.get("reviews_attempt") != task["attempt"]):
                    raise Conflict("Work requires an accepted separate review attempt of this result")
            task.update(status="accepted", token=None, acceptance_evidence=evidence[:4000])
            self._put(db, "tasks", task)
            self._event(db, "task_accepted", ident, evidence)
            return self._public(task)

    def goal_state(self, ident, status, evidence=""):
        if status not in {"active", "paused", "completed"}:
            raise Conflict("Invalid goal state")
        with self.tx() as db:
            self._sweep(db)
            goal = self._get(db, "goals", ident)
            tasks = [t for t in self._all(db, "tasks") if t["goal_id"] == ident]
            if goal["status"] == "completed":
                raise Conflict("Completed goals are immutable; create a new bounded goal")
            if status == "completed" and (len(evidence.strip()) < 10 or not any(t["kind"] == "work" and t["status"] == "accepted" for t in tasks)
                    or any(t["status"] not in {"accepted", "cancelled"} for t in tasks)):
                raise Conflict("Goal needs accepted work, no unresolved tasks, and concrete acceptance evidence")
            goal.update(status=status, evidence=evidence[:4000])
            self._put(db, "goals", goal)
            if status == "paused":
                for task in tasks:
                    if task["status"] == "running":
                        task["status"] = "cancelling"
                        self._put(db, "tasks", task)
            self._event(db, "goal_state", ident, status)
            return goal

    def reserve_advice(self, daily_limit):
        with self.tx() as db:
            start = self.clock() // 86400 * 86400
            used = db.execute("SELECT count(*) FROM events WHERE kind='advisor_call' AND at>=?", (start,)).fetchone()[0]
            if used >= daily_limit:
                raise Conflict("Advisor disabled or UTC daily call budget exhausted")
            self._event(db, "advisor_call", "advisor")
