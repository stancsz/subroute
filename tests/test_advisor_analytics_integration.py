import asyncio
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from test_advisor_plugin import advisor_router, run
from test_advisor_ranked_policy import request
from test_dynamic_routing import make_control_plane
from subroute.plugins import advisor_plugin, dynamic_router

class Recorder:
    def __init__(self, available=True): self.events=[]; self.available=available
    async def record(self, request_id, **markers):
        self.events.append((request_id,markers)); return self.available

@pytest.mark.parametrize("selected",[False,True])
def test_markers_distinguish_eligibility_from_actual_dispatch(monkeypatch,selected):
    from litellm.types.utils import Usage
    plugin=advisor_plugin.AdvisorPlugin(); plugin.analytics=Recorder()
    monkeypatch.setattr(plugin.frequency_gate,"choose",lambda *a:{"consult":selected,"score":.8,"target_rate":.5,"selected_count":int(selected),"eligible_count":1})
    async def invoke(*args):
        assert plugin.analytics.events[-1][1]=={"invoked":True,"provider_attempts":1}
        return "Check recovery.",Usage(prompt_tokens=3,completion_tokens=4,total_tokens=7)
    monkeypatch.setattr(plugin,"_invoke_advisor",invoke)
    data=request();data["metadata"].update(_gateway_analytics_id="owned",_gateway_analytics_ok=True)
    run(plugin,data,"acompletion")
    assert plugin.analytics.events[0]==("owned",{"eligible":True})
    assert any(e[1].get("invoked") for e in plugin.analytics.events)==selected
    assert any(e[1].get("successful") for e in plugin.analytics.events)==selected

@pytest.mark.parametrize("model",["default","not-configured","current"])
def test_routing_counts_intake_and_overwrites_caller_markers(monkeypatch,tmp_path,model):
    plugin=advisor_plugin.AdvisorPlugin();plugin.analytics=Recorder()
    monkeypatch.setattr(advisor_plugin,"advisor_plugin_instance",plugin)
    from subroute.usage_accounting import trace_context
    token=trace_context.set(SimpleNamespace(request_id="server-owned-root"))
    try:
        router=dynamic_router.DynamicRoutingPlugin(make_control_plane(tmp_path))
        data={"model":model,"messages":[{"role":"user","content":"hi"}],"metadata":{"_gateway_analytics_id":"forged","_gateway_analytics_ok":False}}
        if model=="default":asyncio.run(router.async_pre_call_hook({},None,data,"acompletion"))
        else:
            with pytest.raises(HTTPException):asyncio.run(router.async_pre_call_hook({},None,data,"acompletion"))
        assert plugin.analytics.events==[("server-owned-root",{})]
        assert data["metadata"]["_gateway_analytics_id"]=="server-owned-root"
    finally:trace_context.reset(token)

def test_analytics_failure_stops_followup_writes_without_changing_advice(monkeypatch):
    from litellm.types.utils import Usage
    plugin=advisor_plugin.AdvisorPlugin();plugin.analytics=Recorder(False)
    async def invoke(*args):return "Advice",Usage(prompt_tokens=1,completion_tokens=1,total_tokens=2)
    monkeypatch.setattr(plugin,"_invoke_advisor",invoke)
    data=request();data["metadata"].update(_gateway_analytics_id="owned",_gateway_analytics_ok=True)
    run(plugin,data,"acompletion")
    assert len(plugin.analytics.events)==1
    assert data["metadata"]["gateway_advisor"]["status"]=="advice_injected"

@pytest.mark.parametrize("minutes",["30","60","120"])
def test_summary_endpoint_contract_and_window(monkeypatch,minutes):
    from starlette.requests import Request
    class Summary:
        async def summary(self,value):
            assert value==int(minutes)
            return {"total":5,"eligible":2,"selected":1,"invoked":1,"successful":1,"failed":0,"provider_attempts":1,"window_minutes":value,"partial_window":True,"counts_may_be_incomplete":True,"coverage_verified_start_ms":1000,"coverage_start_ms":500}
    monkeypatch.setattr(advisor_plugin.advisor_plugin_instance,"analytics",Summary())
    req=Request({"type":"http","path":"/api/advisor-analytics","scheme":"http","server":("127.0.0.1",4005),"query_string":("minutes="+minutes).encode(),"client":("127.0.0.1",1),"headers":[]})
    result=asyncio.run(dynamic_router.advisor_analytics_state(req))
    assert (result["invoked_requests"],result["eligible_requests"],result["total_requests"])==(1,2,5)
    assert result["coverage"]=={"complete":False,"started_at":"1970-01-01T00:00:01+00:00"}

@pytest.mark.parametrize("minutes",["0","31","abc"])
def test_summary_endpoint_rejects_invalid_window(minutes):
    from starlette.requests import Request
    req=Request({"type":"http","path":"/api/advisor-analytics","scheme":"http","server":("127.0.0.1",4005),"query_string":("minutes="+minutes).encode(),"client":("127.0.0.1",1),"headers":[]})
    with pytest.raises(HTTPException) as caught:asyncio.run(dynamic_router.advisor_analytics_state(req))
    assert caught.value.status_code==422
