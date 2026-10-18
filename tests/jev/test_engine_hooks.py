"""Neutral Engine hooks retain the existing messages-based Agent behavior."""

from types import SimpleNamespace

from fairy.agents.agent.messages import Messages
from fairy.apps.agent_user_interface import AgentUserInterface
from fairy.apps.system import SystemApp
from fairy.controllers.engine import Engine
from fairy.scenarios.scenario import Scenario
from fairy.scenarios.workflow import Workflow, WorkflowStep
from fairy.types import EventRegisterer


class LegacyAgent:
    def __init__(self):
        self.messages = Messages(system_message="Base policy")
        self.workflow = Workflow()
        self.calls = []
        self.runtime_events = []

    def set_time_manager(self, clock):
        self.time_manager = clock

    def run(self, **kwargs):
        self.calls.append(kwargs)

    def record_event(self, event, **fields):
        self.runtime_events.append({"event": event, **fields})


class HookAgent(LegacyAgent):
    def __init__(self):
        super().__init__()
        self.contexts = []
        self.notifications = []

    def add_system_context(self, content):
        self.contexts.append(content)

    def on_environment_event(self, content, **fields):
        self.notifications.append({"content": content, **fields})
        self.workflow.add_node(WorkflowStep(op_type="system", content=content, time=fields.get("time")))


class Notification:
    def __init__(self, time_start, content):
        self.time_start = time_start
        self.content = content
        self.triggered = False
        self.starts = []
        self.step_count = 0

    def start(self, timestamp):
        self.starts.append(timestamp)

    def step(self):
        self.step_count += 1
        return self.content


def scenario():
    return Scenario(start_time=1000.0, apps=[AgentUserInterface(), SystemApp()],
                    scenario_input="Visible fallback task", additional_system_prompt="Scenario policy")


def test_neutral_system_context_hook_is_called_once_and_bypasses_messages():
    agent = HookAgent()
    world = scenario()
    engine = Engine(agent, world)
    engine.run_scenario_agent(setup_scenario=False, max_tool_calls=4, timeout_seconds=10)
    assert agent.contexts == ["Scenario policy"]
    assert agent.messages.messages == [{"role": "system", "content": "Base policy"}]
    assert agent.calls == [{"input": "Visible fallback task", "max_tool_calls": 4, "timeout_seconds": 10}]
    assert all(app.time_manager is engine.time_manager for app in world.apps)
    assert agent.time_manager is engine.time_manager


def test_legacy_system_context_fallback_remains_idempotent():
    agent = LegacyAgent()
    engine = Engine(agent, scenario())
    engine.run_scenario_agent(setup_scenario=False)
    engine._apply_scenario_system_prompt()
    assert agent.messages.messages[0] == {"role": "system", "content": "Base policy\n\nScenario policy"}
    assert len(agent.calls) == 1


def test_environment_hook_records_notification_and_workflow_once():
    agent = HookAgent()
    world = scenario()
    event = Notification(1005.0, {"effect": "ready", "action_id": "irrigation-1"})
    world.dynamic_events = [event]
    engine = Engine(agent, world)
    system = next(app for app in world.apps if isinstance(app, SystemApp))
    system.wait_for_notification(timeout=30)
    system.wait_for_notification(timeout=1)
    assert event.triggered
    assert event.step_count == 1
    assert len(event.starts) == 1
    assert len(agent.notifications) == 1
    assert agent.notifications[0]["content"] == event.content
    assert len(agent.workflow) == 1
    assert next(iter(agent.workflow.dag.values())).content == event.content
    assert len(agent.messages.messages) == 1


def test_legacy_environment_fallback_records_notification_and_workflow_once():
    agent = LegacyAgent()
    world = scenario()
    event = Notification(1005.0, "Irrigation effect is now ready")
    world.dynamic_events = [event]
    engine = Engine(agent, world)
    system = next(app for app in world.apps if isinstance(app, SystemApp))
    system.wait_for_notification(timeout=30)
    system.wait_for_notification(timeout=1)
    assert event.step_count == 1
    assert len(agent.workflow) == 1
    assert len(agent.messages.messages) == 2
    assert agent.messages.messages[1]["content"] == event.content
    assert agent.messages.messages[1]["time"] == next(iter(agent.workflow.dag.values())).time


def test_visible_briefing_excludes_oracle_messages_and_never_executes_events():
    world = scenario()
    aui = world.apps[0]
    with EventRegisterer.capture_mode():
        oracle = aui.send_message_to_agent(content="HIDDEN ORACLE ANSWER").oracle()
        visible = aui.send_message_to_agent(content="Visible irrigation briefing")
        report = aui.send_message_to_user(content="HIDDEN FINAL REPORT").oracle()
    world.events = [oracle, visible, report]
    agent = HookAgent()
    Engine(agent, world).run_scenario_agent(setup_scenario=False)
    assert agent.calls[0]["input"] == "Visible irrigation briefing"
    assert agent.runtime_events[0]["source"] == "event_briefing"
    assert "HIDDEN" not in str(agent.calls)
    assert len(agent.workflow) == 0


def test_hook_agent_does_not_require_messages_attribute():
    agent = HookAgent()
    del agent.messages
    world = scenario()
    engine = Engine(agent, world)
    engine.run_scenario_agent(setup_scenario=False)
    engine._trigger_dynamic_event(Notification(1000.0, "Visible environment notice"))
    assert agent.contexts == ["Scenario policy"]
    assert len(agent.notifications) == 1
    assert len(agent.workflow) == 1
