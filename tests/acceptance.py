"""Acceptance test script — verifies checklist.md items via running API.

Usage: python tests/acceptance.py

Run AFTER restarting the uvicorn server with the latest code.
"""

import asyncio
import httpx
import json
import sys

BASE_URL = "http://127.0.0.1:8000"
PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} -- {detail}")


async def main():
    global PASS, FAIL
    print("=" * 60)
    print("OTC Drug AI System - Acceptance Tests (checklist.md)")
    print("=" * 60)

    async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
        # ── C25: Health Check ──
        print("\n[C25] Health Check")
        resp = await client.get(f"{BASE_URL}/health")
        data = resp.json()
        check("status=ok", data["status"] == "ok", f"got {data['status']}")
        check("postgres=ok", data["postgres"] == "ok", f"got {data['postgres']}")
        check("milvus=ok", data["milvus"] == "ok", f"got {data['milvus']}")
        check("llm=ok", data["llm"] == "ok", f"got {data['llm']}")

        # ── C7: F1 Anonymous Session ──
        print("\n[C7] F1 Anonymous Session")
        resp = await client.post(f"{BASE_URL}/api/v1/sessions")
        check("POST 201", resp.status_code == 201, f"got {resp.status_code}")
        session_data = resp.json()
        session_id = session_data["session_id"]
        check("session_id returned", len(session_id) > 0)
        check("status=active", session_data["status"] == "active")
        check("expires_at set", session_data.get("expires_at") is not None)
        print(f"     session_id: {session_id}")

        # ── C7: Session GET ──
        resp = await client.get(f"{BASE_URL}/api/v1/sessions/{session_id}")
        check("GET session 200", resp.status_code == 200, f"got {resp.status_code}")
        detail = resp.json()
        check("messages list exists", "messages" in detail)

        # ── E2E-1: Standard consult → recommend ──
        print("\n[E2E-1] Standard consult -> recommend flow")
        e2e1_session = session_id
        e2e1_msgs = [
            "我头疼，有点发烧，两天了",
            "38度，没有过敏，28岁",
            "没有其他症状了",
            "没有",
            "没了",
            "确实没有了",
        ]
        e2e1_has_recommend = False
        e2e1_has_done = False
        for i, msg in enumerate(e2e1_msgs, 1):
            resp1 = await client.post(
                f"{BASE_URL}/api/v1/chat/{e2e1_session}",
                json={"message": msg}
            )
            text = resp1.text
            if "event: done" in text:
                e2e1_has_done = True
            if "recommend" in text.lower() or "safety" in text.lower():
                e2e1_has_recommend = True
                print(f"     Round {i}: Found recommendation/safety event (size={len(text)} chars)")
                break
            print(f"     Round {i}: consult follow-up (size={len(text)} chars)")
        check("E2E-1 SSE events present", e2e1_has_done)
        check("E2E-1 reached recommend or safety", e2e1_has_recommend)

        # ── E2E-2: Safety block ──
        print("\n[E2E-2] Safety block scenario")
        resp = await client.post(f"{BASE_URL}/api/v1/sessions")
        danger_sid = resp.json()["session_id"]
        danger_msgs = [
            "我发烧39.5度，烧了四天了",
            "没有",
            "没有了",
            "28岁",
            "没有过敏",
            "确实没有",
        ]
        safety_blocked = False
        for i, msg in enumerate(danger_msgs, 1):
            resp1 = await client.post(
                f"{BASE_URL}/api/v1/chat/{danger_sid}",
                json={"message": msg}
            )
            text = resp1.text
            if "BLOCK" in text or "safety" in text.lower():
                safety_blocked = True
                print(f"     Round {i}: SAFETY BLOCK triggered!")
                break
            print(f"     Round {i}: consult follow-up (size={len(text)} chars)")
        check("E2E-2 Safety BLOCK triggered", safety_blocked)

        # ── E2E-3: Topic switch ──
        print("\n[E2E-3] Topic switch during consult")
        resp = await client.post(f"{BASE_URL}/api/v1/sessions")
        switch_sid = resp.json()["session_id"]
        print(f"     switch session: {switch_sid}")

        await client.post(
            f"{BASE_URL}/api/v1/chat/{switch_sid}",
            json={"message": "我咳嗽流鼻涕"}
        )
        resp2 = await client.post(
            f"{BASE_URL}/api/v1/chat/{switch_sid}",
            json={"message": "布洛芬有什么副作用？"}
        )
        switch_text = resp2.text
        check("Switch to explain", resp2.status_code in (200, 201))
        # Should contain drug info
        has_drug = "布洛芬" in switch_text or "ibuprofen" in switch_text.lower()
        print(f"     Drug info found: {has_drug}")

        # ── C9: Session GET history ──
        print("\n[C9] Session history")
        resp = await client.get(f"{BASE_URL}/api/v1/sessions/{session_id}")
        check("GET session 200", resp.status_code == 200, f"got {resp.status_code}")
        detail = resp.json()
        msg_count = len(detail.get("messages", []))
        check("Has messages in history", msg_count > 0, f"found {msg_count} messages")
        print(f"     Messages in session: {msg_count}")

    # ── Summary ──
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"Results: {PASS}/{total} passed, {FAIL}/{total} failed")
    if FAIL == 0:
        print("*** All acceptance tests passed! ***")
    else:
        print("WARNING: Some tests failed. Check the output above.")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
