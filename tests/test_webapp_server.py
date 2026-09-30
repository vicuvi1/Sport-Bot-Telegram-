import asyncio
from pathlib import Path
from aiohttp.test_utils import TestServer, TestClient

from database import init_db
from webapp.server import create_webapp

def test_webapp_routes(tmp_path: Path):
    async def run_tests():
        db_file = tmp_path / "test_webapp.db"
        init_db(db_file)

        app = create_webapp(db_path=db_file)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        try:
            # 1. Test GET /health
            resp = await client.get("/health")
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "healthy"

            # 2. Test GET / (index.html)
            resp_index = await client.get("/")
            assert resp_index.status == 200
            html = await resp_index.text()
            assert "Workout Tracker Mini App" in html
            assert "progress-dial" in html

            # 3. Test GET /api/workout
            resp_workout = await client.get("/api/workout")
            assert resp_workout.status == 200
            workout_json = await resp_workout.json()
            assert "items" in workout_json
            assert len(workout_json["items"]) > 0

            # 4. Test POST /api/workout/item
            item_id = workout_json["items"][0]["id"]
            resp_update = await client.post("/api/workout/item", json={
                "item_id": item_id,
                "delta_reps": 10
            })
            assert resp_update.status == 200
            update_json = await resp_update.json()
            assert update_json["status"] == "ok"
            assert update_json["item"]["completed_reps"] == 10

            # 5. Test POST /api/workout/complete_all
            resp_complete = await client.post("/api/workout/complete_all")
            assert resp_complete.status == 200
            complete_json = await resp_complete.json()
            assert complete_json["status"] == "ok"
            assert complete_json["workout"]["status"] == "completed"
        finally:
            await client.close()

    asyncio.run(run_tests())
