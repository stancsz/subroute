"use strict";
const native = window.subroute;
const form = document.querySelector("#connect-form");
const input = document.querySelector("#gateway-url");
const status = document.querySelector("#connect-status");
const button = document.querySelector("#connect-button");

const params = new URLSearchParams(location.search);
input.value = params.get("gatewayUrl") || "http://127.0.0.1:4000";
if (params.has("error")) status.textContent = `Could not reach ${input.value}: ${params.get("error")}.`;
if (native?.currentGateway) native.currentGateway().then(value => { if (value && !params.has("gatewayUrl")) input.value = value; }).catch(() => {});

form.addEventListener("submit", async event => {
  event.preventDefault();
  status.textContent = "Checking the local gateway…";
  status.classList.remove("error");
  status.classList.add("pending");
  button.disabled = true;
  try {
    if (!native?.connectGateway) throw new Error("Open this page in Subroute Desktop to connect a gateway.");
    await native.connectGateway({ gatewayUrl: input.value });
  } catch (error) {
    status.classList.remove("pending");
    status.classList.add("error");
    status.textContent = error.message || "Could not connect. Check the address and try again.";
    button.disabled = false;
    input.focus();
  }
});
