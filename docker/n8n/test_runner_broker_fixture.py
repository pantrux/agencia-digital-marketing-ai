"""Pruebas locales con clientes HTTP y WebSocket simulados."""

import asyncio
import json
import unittest

from runner_broker_fixture import BrokerFixture
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

TEST_AUTH = "synthetic-fixture-authentication-only"


class BrokerFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.broker = await self.enterAsyncContext(BrokerFixture(TEST_AUTH, connection_seconds=2))

    async def http(self, method, path, body=b"", headers=()):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.broker.port)
        try:
            request = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n".encode()
            request += b"".join(f"{key}: {value}\r\n".encode() for key, value in headers)
            writer.write(request + b"\r\n" + body)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 3)
            head, content = raw.split(b"\r\n\r\n", 1)
            return int(head.split(b" ")[1]), json.loads(content)
        finally:
            writer.close()
            await writer.wait_closed()

    async def auth(self, token=TEST_AUTH):
        body = json.dumps({"token": token}).encode()
        return await self.http(
            "POST",
            "/runners/auth",
            body,
            (("Content-Length", str(len(body))), ("Content-Type", "application/json")),
        )

    def ws(self, token, id="9a4e7cdbcc32f116"):
        return connect(
            f"ws://127.0.0.1:{self.broker.port}/runners/_ws?id={id}",
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=2,
            close_timeout=1,
            proxy=None,
        )

    async def register(self, ws, name):
        self.assertEqual(json.loads(await ws.recv()), {"type": "broker:inforequest"})
        await ws.send(json.dumps({"type": "runner:info", "name": name, "types": ["python"]}))
        self.assertEqual(json.loads(await ws.recv()), {"type": "broker:runnerregistered"})

    async def offer(self, ws, validity, offer="fixtureOffer1"):
        await ws.send(
            json.dumps(
                {
                    "type": "runner:taskoffer",
                    "taskType": "python",
                    "offerId": offer,
                    "validFor": validity,
                }
            )
        )
        reply = json.loads(await ws.recv())
        self.assertEqual((reply["type"], reply["offerId"]), ("broker:taskofferaccept", offer))
        return reply["taskId"]

    async def launch(self):
        status, result = await self.auth()
        self.assertEqual(status, 200)
        token = result["data"]["token"]
        async with self.ws(token) as ws:
            await self.register(ws, "launcher-python")
            task_id = await self.offer(ws, -1)
            await ws.send(json.dumps({"type": "runner:taskdeferred", "taskId": task_id}))
            with self.assertRaises(ConnectionClosed):
                await ws.recv()
        return token, task_id

    async def test_launcher_then_python_and_one_fixed_task(self):
        self.assertEqual(await self.http("GET", "/healthz"), (200, {"status": "ok"}))
        launcher_grant, task_id = await self.launch()
        status, result = await self.auth()
        self.assertEqual(status, 200)
        runner_grant = result["data"]["token"]
        self.assertNotEqual(launcher_grant, runner_grant)
        async with self.ws(runner_grant, "PythonRunnerFixture22") as ws:
            await self.register(ws, "Python Task Runner")
            self.assertEqual(await self.offer(ws, 1000, "runnerOffer2"), task_id)
            await ws.send(
                '{"type":"runner:taskoffer","taskType":"python","offerId":"runnerOffer3","validFor":1000}'
            )
            await ws.send(json.dumps({"type": "runner:taskaccepted", "taskId": task_id}))
            settings = json.loads(await ws.recv())
            self.assertEqual(settings["type"], "broker:tasksettings")
            self.assertEqual(settings["settings"]["items"], [{"json": {"value": 21}}])
            self.assertEqual(
                settings["settings"]["code"],
                'return [{"json": {"value": _items[0]["json"]["value"] * 2}}]',
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "runner:taskdone",
                        "taskId": task_id,
                        "data": {"result": [{"json": {"value": 42}}]},
                    }
                )
            )
            self.assertEqual(json.loads(await ws.recv()), {"type": "broker:drain"})
        self.assertTrue(await self.broker.wait(1))
        result = self.broker.result()
        self.assertEqual(
            result["events"],
            [
                "launcher_registered",
                "launcher_deferred",
                "python_registered",
                "python_task_accepted",
                "python_task_done",
            ],
        )
        self.assertEqual(result["grants_issued"], 2)
        self.assertNotIn(runner_grant, json.dumps(result))
        self.assertNotIn(TEST_AUTH, json.dumps(result))
        self.assertEqual((await self.auth())[0], 409)

    async def test_wrong_python_task_result_is_a_protocol_failure(self):
        _, task_id = await self.launch()
        _, result = await self.auth()
        async with self.ws(result["data"]["token"], "PythonRunnerFixture22") as ws:
            await self.register(ws, "Python Task Runner")
            self.assertEqual(await self.offer(ws, 1000), task_id)
            await ws.send(json.dumps({"type": "runner:taskaccepted", "taskId": task_id}))
            self.assertEqual(json.loads(await ws.recv())["type"], "broker:tasksettings")
            await ws.send(
                json.dumps(
                    {
                        "type": "runner:taskdone",
                        "taskId": task_id,
                        "data": {"result": [{"json": {"value": 41}}]},
                    }
                )
            )
            with self.assertRaises(ConnectionClosed):
                await ws.recv()
        self.assertFalse(await self.broker.wait(1))
        self.assertEqual(self.broker.result()["error"], "protocol_python")

    async def test_auth_framing_and_wrong_token_do_not_issue_grant(self):
        self.assertEqual((await self.auth("incorrect-test-token"))[0], 401)
        body = b'{"token":"a","token":"b"}'
        headers = (("Content-Length", str(len(body))), ("Content-Type", "application/json"))
        self.assertEqual((await self.http("POST", "/runners/auth", body, headers))[0], 400)
        self.assertEqual(
            (await self.http("POST", "/runners/auth", body, (*headers, headers[0])))[0], 400
        )
        self.assertEqual(
            (await self.http("POST", "/runners/auth", b"", (("Content-Length", "513"),)))[0], 413
        )
        self.assertEqual((await self.http("GET", "/healthz?redirect=x"))[0], 404)
        self.assertEqual(self.broker.result()["grants_issued"], 0)

    async def test_grant_replay_rejected_and_invalid_token_does_not_consume_valid_one(self):
        with self.assertRaises(InvalidStatus) as invalid:
            async with self.ws("unissued-token"):
                self.fail("Se aceptó un grant sin emitir")
        self.assertEqual(invalid.exception.response.status_code, 403)
        token, _ = await self.launch()
        with self.assertRaises(InvalidStatus) as replay:
            async with self.ws(token):
                self.fail("Se reutilizó un grant")
        self.assertEqual(replay.exception.response.status_code, 403)
        self.assertEqual(self.broker.result()["grants_consumed"], 1)

    async def test_invalid_launcher_role_fails_without_issuing_python_grant(self):
        _, result = await self.auth()
        async with self.ws(result["data"]["token"]) as ws:
            self.assertEqual(json.loads(await ws.recv()), {"type": "broker:inforequest"})
            await ws.send(
                '{"type":"runner:info","name":"launcher-javascript","types":["javascript"]}'
            )
            with self.assertRaises(ConnectionClosed):
                await ws.recv()
        self.assertFalse(await self.broker.wait(1))
        self.assertEqual((await self.auth())[0], 409)
        self.assertEqual(self.broker.result()["error"], "protocol_launcher")

    async def test_timeout_closes_incomplete_http_request(self):
        async with BrokerFixture(TEST_AUTH, connection_seconds=0.15) as broker:
            reader, writer = await asyncio.open_connection("127.0.0.1", broker.port)
            try:
                writer.write(b"GET /healthz HTTP/1.1\r\n")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
                self.assertEqual(broker.result()["grants_issued"], 0)
            finally:
                writer.close()
                await writer.wait_closed()

    async def test_fixture_close_cancels_only_its_active_connections(self):
        broker = BrokerFixture(TEST_AUTH, connection_seconds=25)
        async with broker:
            reader, writer = await asyncio.open_connection("127.0.0.1", broker.port)
            writer.write(b"GET /healthz HTTP/1.1\r\n")
            await writer.drain()
        try:
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        finally:
            writer.close()
            await writer.wait_closed()
        self.assertEqual(await self.http("GET", "/healthz"), (200, {"status": "ok"}))
