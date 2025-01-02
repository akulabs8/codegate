import asyncio
import json
from pathlib import Path
from typing import List, Optional

import structlog
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from codegate.db.fim_cache import FimCache
from codegate.db.models import Alert, Output, Prompt
from codegate.db.queries import (
    AsyncQuerier,
    GetAlertsWithPromptAndOutputRow,
    GetPromptWithOutputsRow,
)
from codegate.pipeline.base import PipelineContext

logger = structlog.get_logger("codegate")
alert_queue = asyncio.Queue()
fim_cache = FimCache()


class DbCodeGate:

    def __init__(self, sqlite_path: Optional[str] = None):
        # Initialize SQLite database engine with proper async URL
        if not sqlite_path:
            current_dir = Path(__file__).parent
            sqlite_path = (
                current_dir.parent.parent.parent / "codegate_volume" / "db" / "codegate.db"
            )
        self._db_path = Path(sqlite_path).absolute()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Initializing DB from path: {self._db_path}")
        engine_dict = {
            "url": f"sqlite+aiosqlite:///{self._db_path}",
            "echo": False,  # Set to False in production
            "isolation_level": "AUTOCOMMIT",  # Required for SQLite
        }
        self._async_db_engine = create_async_engine(**engine_dict)

    def does_db_exist(self):
        return self._db_path.is_file()


class DbRecorder(DbCodeGate):

    def __init__(self, sqlite_path: Optional[str] = None):
        super().__init__(sqlite_path)

        if not self.does_db_exist():
            logger.info(f"Database does not exist at {self._db_path}. Creating..")
            asyncio.run(self.init_db())

    async def init_db(self):
        """Initialize the database with the schema."""
        if self.does_db_exist():
            logger.info("Database already exists. Skipping initialization.")
            return

        # Get the absolute path to the schema file
        current_dir = Path(__file__).parent
        schema_path = current_dir.parent.parent.parent / "sql" / "schema" / "schema.sql"

        if not schema_path.exists():
            raise FileNotFoundError(f"Schema file not found at {schema_path}")

        # Read the schema
        with open(schema_path, "r") as f:
            schema = f.read()

        try:
            # Execute the schema
            async with self._async_db_engine.begin() as conn:
                # Split the schema into individual statements and execute each one
                statements = [stmt.strip() for stmt in schema.split(";") if stmt.strip()]
                for statement in statements:
                    # Use SQLAlchemy text() to create executable SQL statements
                    await conn.execute(text(statement))
        finally:
            await self._async_db_engine.dispose()

    async def _insert_pydantic_model(
        self, model: BaseModel, sql_insert: text
    ) -> Optional[BaseModel]:
        # There are create method in queries.py automatically generated by sqlc
        # However, the methods are buggy for Pydancti and don't work as expected.
        # Manually writing the SQL query to insert Pydantic models.
        async with self._async_db_engine.begin() as conn:
            try:
                result = await conn.execute(sql_insert, model.model_dump())
                row = result.first()
                if row is None:
                    return None

                # Get the class of the Pydantic object to create a new object
                model_class = model.__class__
                return model_class(**row._asdict())
            except Exception as e:
                logger.error(f"Failed to insert model: {model}.", error=str(e))
                return None

    async def record_request(self, prompt_params: Optional[Prompt] = None) -> Optional[Prompt]:
        if prompt_params is None:
            return None
        sql = text(
            """
                INSERT INTO prompts (id, timestamp, provider, request, type)
                VALUES (:id, :timestamp, :provider, :request, :type)
                RETURNING *
                """
        )
        recorded_request = await self._insert_pydantic_model(prompt_params, sql)
        # Uncomment to debug the recorded request
        # logger.debug(f"Recorded request: {recorded_request}")
        return recorded_request

    async def record_outputs(self, outputs: List[Output]) -> Optional[Output]:
        if not outputs:
            return

        first_output = outputs[0]
        # Create a single entry on DB but encode all of the chunks in the stream as a list
        # of JSON objects in the field `output`
        output_db = Output(
            id=first_output.id,
            prompt_id=first_output.prompt_id,
            timestamp=first_output.timestamp,
            output=first_output.output,
        )
        full_outputs = []
        # Just store the model respnses in the list of JSON objects.
        for output in outputs:
            full_outputs.append(output.output)
        output_db.output = json.dumps(full_outputs)

        sql = text(
            """
                INSERT INTO outputs (id, prompt_id, timestamp, output)
                VALUES (:id, :prompt_id, :timestamp, :output)
                RETURNING *
                """
        )
        recorded_output = await self._insert_pydantic_model(output_db, sql)
        # Uncomment to debug
        # logger.debug(f"Recorded output: {recorded_output}")
        return recorded_output

    async def record_alerts(self, alerts: List[Alert]) -> List[Alert]:
        if not alerts:
            return
        sql = text(
            """
                INSERT INTO alerts (
                id, prompt_id, code_snippet, trigger_string, trigger_type, trigger_category,
                timestamp
                )
                VALUES (:id, :prompt_id, :code_snippet, :trigger_string, :trigger_type,
                :trigger_category, :timestamp)
                RETURNING *
                """
        )
        # We can insert each alert independently in parallel.
        alerts_tasks = []
        async with asyncio.TaskGroup() as tg:
            for alert in alerts:
                try:
                    result = tg.create_task(self._insert_pydantic_model(alert, sql))
                    alerts_tasks.append(result)
                except Exception as e:
                    logger.error(f"Failed to record alert: {alert}.", error=str(e))

        recorded_alerts = []
        for alert_coro in alerts_tasks:
            alert_result = alert_coro.result()
            recorded_alerts.append(alert_result)
            if alert_result and alert_result.trigger_category == "critical":
                await alert_queue.put(f"New alert detected: {alert.timestamp}")
        # Uncomment to debug the recorded alerts
        # logger.debug(f"Recorded alerts: {recorded_alerts}")
        return recorded_alerts

    def _should_record_context(self, context: Optional[PipelineContext]) -> bool:
        """Check if the context should be recorded in DB"""
        if context is None or context.metadata.get("stored_in_db", False):
            return False

        if not context.input_request:
            logger.warning("No input request found. Skipping recording context.")
            return False

        # If it's not a FIM prompt, we don't need to check anything else.
        if context.input_request.type != "fim":
            return True

        return fim_cache.could_store_fim_request(context)

    async def record_context(self, context: Optional[PipelineContext]) -> None:
        try:
            if not self._should_record_context(context):
                return
            await self.record_request(context.input_request)
            await self.record_outputs(context.output_responses)
            await self.record_alerts(context.alerts_raised)
            context.metadata["stored_in_db"] = True
            logger.info(
                f"Recorded context in DB. Output chunks: {len(context.output_responses)}. "
                f"Alerts: {len(context.alerts_raised)}."
            )
        except Exception as e:
            logger.error(f"Failed to record context: {context}.", error=str(e))


class DbReader(DbCodeGate):

    def __init__(self, sqlite_path: Optional[str] = None):
        super().__init__(sqlite_path)

    async def get_prompts_with_output(self) -> List[GetPromptWithOutputsRow]:
        conn = await self._async_db_engine.connect()
        querier = AsyncQuerier(conn)
        prompts = [prompt async for prompt in querier.get_prompt_with_outputs()]
        await conn.close()
        return prompts

    async def get_alerts_with_prompt_and_output(self) -> List[GetAlertsWithPromptAndOutputRow]:
        conn = await self._async_db_engine.connect()
        querier = AsyncQuerier(conn)
        prompts = [prompt async for prompt in querier.get_alerts_with_prompt_and_output()]
        await conn.close()
        return prompts


def init_db_sync(db_path: Optional[str] = None):
    """DB will be initialized in the constructor in case it doesn't exist."""
    db = DbRecorder(db_path)
    asyncio.run(db.init_db())


if __name__ == "__main__":
    init_db_sync()
