#!/usr/bin/env python3
"""
DreamEngine — ROS2 Humble node for nocturnal memory consolidation.

Activates when /cognitive_state = "dreaming" is received.
Runs the full consolidation pipeline and publishes /dream_state updates
so CognitiveRenderer can render dream phases on NAO's LEDs.

Consolidation pipeline:
  1. Load raw episodes from ChromaDB "episodes_raw"
  2. Apply Ebbinghaus decay: R(t) = importance × e^(-t/S)
  3. Batch-consolidate via Qwen3 → semantic summaries → "long_term"
  4. Generate proto-actions (hypothetical scenarios) → "proto_actions"
  5. Build morning_state (mood, priority topics, learned behaviours) → "morning_state"
  6. Clean processed raw episodes

During dreaming NAO is rendered visually:
  /dream_state = "loading_memories" | "analysing" | "consolidating" |
                 "generating_scenarios" | "storing" | "complete"

Environment variables:
  OLLAMA_URL                default http://localhost:11434
  CHROMA_HOST               default localhost
  CHROMA_PORT               default 8000
  CONSOLIDATION_BATCH_SIZE  default 5
"""

import datetime
import json
import math
import os
import re
import threading
import time

import chromadb
import requests
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "localhost")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
BATCH_SIZE = int(os.environ.get("CONSOLIDATION_BATCH_SIZE", "5"))


# ──────────────────────────────────────────────────────────────────────────────
# Ebbinghaus decay
# ──────────────────────────────────────────────────────────────────────────────

def ebbinghaus_decay(importance: float, age_hours: float, stability: float) -> float:
    """R(t) = importance × e^(-t/S).  stability grows with reactivation count."""
    return importance * math.exp(-age_hours / max(stability, 1.0))


# ──────────────────────────────────────────────────────────────────────────────
# DreamEngine node
# ──────────────────────────────────────────────────────────────────────────────

class DreamEngineNode(Node):

    def __init__(self):
        super().__init__("dream_engine")

        self._dreaming = False
        self._dream_thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self._chroma = self._init_chroma()

        # ── Subscriber — waits for brain to enter DREAMING state ───────────────
        self.create_subscription(String, "/cognitive_state", self._on_cognitive_state, 10)

        # ── Publishers ─────────────────────────────────────────────────────────
        self.pub_dream_state = self.create_publisher(String, "/dream_state", 10)

        self.get_logger().info("DreamEngine ready")

    # ──────────────────────────────────────────────────────────────────────────
    # ChromaDB
    # ──────────────────────────────────────────────────────────────────────────

    def _init_chroma(self):
        try:
            client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
            for name in ("episodes_raw", "long_term", "proto_actions",
                         "morning_state", "interaction_ctx"):
                client.get_or_create_collection(name)
            self.get_logger().info("ChromaDB connected")
            return client
        except Exception as e:
            self.get_logger().warn(f"ChromaDB unavailable: {e}")
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # State listener
    # ──────────────────────────────────────────────────────────────────────────

    def _on_cognitive_state(self, msg: String):
        state = msg.data.strip().lower()

        if state == "dreaming" and not self._dreaming:
            with self._lock:
                self._dreaming = True
            self.get_logger().info("Dream cycle starting")
            self._dream_thread = threading.Thread(
                target=self._dream_cycle, daemon=True
            )
            self._dream_thread.start()

        elif state != "dreaming" and self._dreaming:
            with self._lock:
                self._dreaming = False
            self.get_logger().info("Dream cycle interrupted — waking")

    # ──────────────────────────────────────────────────────────────────────────
    # Dream cycle pipeline
    # ──────────────────────────────────────────────────────────────────────────

    def _dream_cycle(self):
        start = time.time()
        try:
            self._publish_dream("loading_memories")
            episodes = self._load_raw_episodes()

            if not episodes["ids"]:
                self.get_logger().info("No episodes to consolidate")
                self._publish_dream("complete")
                return

            self.get_logger().info(f"Consolidating {len(episodes['ids'])} episodes")

            self._publish_dream("analysing")
            decayed = self._apply_decay(episodes)

            self._publish_dream("consolidating")
            summaries = self._consolidate_with_llm(decayed)
            self._store_long_term(summaries)

            self._publish_dream("generating_scenarios")
            proto = self._generate_proto_actions(summaries)
            self._store_proto_actions(proto)

            self._publish_dream("storing")
            morning = self._build_morning_state(summaries, proto)
            self._store_morning_state(morning)
            self._cleanup_episodes(episodes["ids"])

            elapsed = time.time() - start
            self.get_logger().info(f"Dream cycle complete in {elapsed:.1f}s")
            self._publish_dream("complete")

        except Exception as e:
            self.get_logger().error(f"Dream cycle error: {e}")
            self._publish_dream("complete")
        finally:
            with self._lock:
                self._dreaming = False

    # ──────────────────────────────────────────────────────────────────────────
    # Step 1 — load raw episodes
    # ──────────────────────────────────────────────────────────────────────────

    def _load_raw_episodes(self) -> dict:
        if not self._chroma:
            return {"ids": [], "documents": [], "metadatas": []}
        try:
            col = self._chroma.get_collection("episodes_raw")
            return col.get(include=["documents", "metadatas"])
        except Exception as e:
            self.get_logger().error(f"Load episodes error: {e}")
            return {"ids": [], "documents": [], "metadatas": []}

    # ──────────────────────────────────────────────────────────────────────────
    # Step 2 — Ebbinghaus decay filter
    # ──────────────────────────────────────────────────────────────────────────

    def _apply_decay(self, episodes: dict) -> list[dict]:
        now = datetime.datetime.utcnow()
        result = []

        for doc_id, doc, meta in zip(
            episodes["ids"], episodes["documents"], episodes["metadatas"]
        ):
            try:
                ts_raw = meta.get("timestamp", now.isoformat())
                ts = datetime.datetime.fromisoformat(ts_raw)
                age_hours = (now - ts).total_seconds() / 3600.0
                importance = float(meta.get("importance", "0.5"))
                reactivation = int(meta.get("reactivation_count", "0"))
                # Higher reactivation → longer stability
                stability = 24.0 * (1.0 + reactivation * 0.5)
                decayed = ebbinghaus_decay(importance, age_hours, stability)

                if decayed > 0.1:
                    result.append({
                        "id": doc_id,
                        "document": doc,
                        "metadata": meta,
                        "decayed_importance": decayed,
                    })
            except Exception as e:
                self.get_logger().warn(f"Decay error for {doc_id}: {e}")

        result.sort(key=lambda x: x["decayed_importance"], reverse=True)
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # Step 3 — LLM consolidation
    # ──────────────────────────────────────────────────────────────────────────

    def _consolidate_with_llm(self, episodes: list[dict]) -> list[dict]:
        summaries = []

        for i in range(0, min(len(episodes), 50), BATCH_SIZE):
            if not self._dreaming:
                break  # woken up mid-dream

            batch = episodes[i : i + BATCH_SIZE]
            docs = "\n---\n".join(e["document"] for e in batch)

            prompt = (
                "Analyse ces épisodes d'interaction et crée un résumé sémantique concis.\n"
                "Extrais: thèmes clés, comportements appris, faits importants, patterns émotionnels.\n\n"
                f"Épisodes:\n{docs}\n\n"
                "Réponds en JSON:\n"
                '{"summary": "...", "themes": ["..."], "learned": ["..."], "importance": 0.0}'
            )

            try:
                resp = requests.post(
                    f"{OLLAMA_URL}/api/generate",
                    json={
                        "model": "qwen3:8b",
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 400},
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                raw = resp.json()["response"]
                parsed = self._extract_json(raw, default={
                    "summary": raw[:200], "themes": [], "learned": [], "importance": 0.5
                })
                avg_importance = sum(e["decayed_importance"] for e in batch) / len(batch)
                summaries.append({
                    "content": parsed,
                    "source_ids": [e["id"] for e in batch],
                    "avg_importance": avg_importance,
                })
            except Exception as e:
                self.get_logger().error(f"LLM consolidation error (batch {i}): {e}")

        return summaries

    # ──────────────────────────────────────────────────────────────────────────
    # Step 4 — proto-actions generation
    # ──────────────────────────────────────────────────────────────────────────

    def _generate_proto_actions(self, summaries: list[dict]) -> list[dict]:
        if not summaries:
            return []

        combined = "\n".join(json.dumps(s["content"]) for s in summaries[:5])
        prompt = (
            "À partir de ces souvenirs consolidés, génère 3 scénarios proto-actions.\n"
            "Chaque scénario est une situation hypothétique et la réponse recommandée.\n\n"
            f"Souvenirs:\n{combined}\n\n"
            "Réponds en JSON array:\n"
            '[{"situation": "...", "recommended_action": "...", "confidence": 0.0}]'
        )

        try:
            resp = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": "qwen3:8b",
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.5, "num_predict": 500},
                },
                timeout=60,
            )
            resp.raise_for_status()
            raw = resp.json()["response"]
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception as e:
            self.get_logger().error(f"Proto-actions generation error: {e}")

        return []

    # ──────────────────────────────────────────────────────────────────────────
    # Steps 5-7 — store and cleanup
    # ──────────────────────────────────────────────────────────────────────────

    def _store_long_term(self, summaries: list[dict]):
        if not self._chroma or not summaries:
            return
        try:
            col = self._chroma.get_collection("long_term")
            for idx, s in enumerate(summaries):
                doc_id = f"lt_{int(time.time())}_{idx}"
                col.add(
                    documents=[json.dumps(s["content"])],
                    metadatas=[{
                        "timestamp": datetime.datetime.utcnow().isoformat(),
                        "importance": str(s["avg_importance"]),
                        "source_count": str(len(s["source_ids"])),
                    }],
                    ids=[doc_id],
                )
        except Exception as e:
            self.get_logger().error(f"Store long_term error: {e}")

    def _store_proto_actions(self, proto: list[dict]):
        if not self._chroma or not proto:
            return
        try:
            col = self._chroma.get_collection("proto_actions")
            for idx, p in enumerate(proto):
                doc_id = f"pa_{int(time.time())}_{idx}"
                col.add(
                    documents=[json.dumps(p)],
                    metadatas=[{"timestamp": datetime.datetime.utcnow().isoformat()}],
                    ids=[doc_id],
                )
        except Exception as e:
            self.get_logger().error(f"Store proto_actions error: {e}")

    def _build_morning_state(self, summaries: list[dict], proto: list[dict]) -> dict:
        themes: list[str] = []
        learned: list[str] = []
        forgotten = 0

        for s in summaries:
            content = s["content"]
            themes.extend(content.get("themes", []))
            learned.extend(content.get("learned", []))
            if s["avg_importance"] < 0.2:
                forgotten += 1

        return {
            "mood": "curious" if themes else "neutral",
            "priority_topics": list(dict.fromkeys(themes))[:5],
            "learned_behaviors": learned[:10],
            "forgotten": forgotten,
            "proto_action_count": len(proto),
            "generated_at": datetime.datetime.utcnow().isoformat(),
        }

    def _store_morning_state(self, morning: dict):
        if not self._chroma:
            return
        try:
            col = self._chroma.get_collection("morning_state")
            existing = col.get()
            if existing["ids"]:
                col.delete(ids=existing["ids"])
            col.add(
                documents=[json.dumps(morning)],
                metadatas=[{"timestamp": datetime.datetime.utcnow().isoformat()}],
                ids=["morning_state_current"],
            )
            self.get_logger().info(f"Morning state stored: mood={morning['mood']}")
        except Exception as e:
            self.get_logger().error(f"Store morning_state error: {e}")

    def _cleanup_episodes(self, ids: list[str]):
        if not self._chroma or not ids:
            return
        try:
            col = self._chroma.get_collection("episodes_raw")
            col.delete(ids=ids)
            self.get_logger().info(f"Cleaned {len(ids)} raw episodes")
        except Exception as e:
            self.get_logger().error(f"Cleanup error: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _publish_dream(self, phase: str):
        msg = String()
        msg.data = phase
        self.pub_dream_state.publish(msg)
        self.get_logger().info(f"Dream phase → {phase}")

    def _extract_json(self, raw: str, default: dict) -> dict:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return default


def main():
    rclpy.init()
    node = DreamEngineNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
