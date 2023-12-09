import asyncio
import json
import time
from io import IOBase
from typing import List, Dict, Any, Type, Optional

from openai import AsyncOpenAI
from openai.types.beta import Assistant
from openai.types.beta.assistant_create_params import ToolAssistantToolsFunction
from openai.types.beta.threads import ThreadMessage
from openai.types.beta.threads.run_submit_tool_outputs_params import ToolOutput
from pydantic import Field

from eidos.agent_os import AgentOS
from eidos.cpu.agent_cpu import AgentCPUSpec, AgentCPU, Thread
from eidos.cpu.agent_io import CPUMessageTypes
from eidos.cpu.call_context import CallContext
from eidos.cpu.llm_message import ToolResponseMessage, LLMMessage
from eidos.cpu.logic_unit import ToolDefType, LogicUnit
from eidos.cpu.processing_unit import ProcessingUnitLocator, PU_T
from eidos.system.reference_model import Specable, Reference


class OpenAIAssistantsCPUSpec(AgentCPUSpec):
    logic_units: List[Reference[LogicUnit]] = []
    model: str = Field(default="gpt-4-1106-preview", description="The model to use for the LLM.")
    temperature: float = 0.3
    max_wait_time_secs: int = 600
    llm_poll_interval_ms: int = 500
    enable_retrieval: bool = True
    enable_code_interpreter: bool = True


class OpenAIAssistantsCPU(AgentCPU, Specable[OpenAIAssistantsCPUSpec], ProcessingUnitLocator):
    llm: AsyncOpenAI = None
    logic_units: List[LogicUnit] = None

    def __init__(self, spec: OpenAIAssistantsCPUSpec = None):
        super().__init__(spec)
        self.tool_defs = None
        kwargs = dict(processing_unit_locator=self)
        self.logic_units = [logic_unit.instantiate(**kwargs) for logic_unit in self.spec.logic_units]

    def locate_unit(self, unit_type: Type[PU_T]) -> Optional[PU_T]:
        for unit in self.logic_units:
            if isinstance(unit, unit_type):
                return unit
        raise ValueError(f"Could not locate {unit_type}")

    def _getLLM(self):
        if not self.llm:
            self.llm = AsyncOpenAI()
        return self.llm

    async def processFile(self, prompt: CPUMessageTypes) -> str:
        # rip out the image messages, store them in the file system, and replace them file Ids
        # collect the user messages
        llm = self._getLLM()
        image_file: IOBase = prompt.image
        # read the prompt.image file into memory
        image_data = image_file.read()
        file = await llm.files.create(file=image_data, purpose="assistants")
        return file.id

    async def get_or_create_assistant(self, call_context: CallContext, system_message: str = "", file_ids=None) -> (Assistant, str):
        # fetch the existing conversation from symbolic memory
        existingConversation = await AgentOS.symbolic_memory.find_one("open_ai_conversations", {
            "process_id": call_context.process_id,
            "thread_id": call_context.thread_id
        })

        llm = self._getLLM()
        if existingConversation:
            assistant_thread_id = existingConversation["assistant_thread_id"]
            assistant = await llm.beta.assistants.retrieve(existingConversation["assistant_id"])
            return assistant, assistant_thread_id

        request = {
            "model": self.spec.model
        }
        if len(system_message) > 0:
            request["instructions"] = system_message

        if file_ids and len(file_ids) > 0:
            request["file_ids"] = file_ids

        # todo -- add tools from defs
        tools = []
        if self.spec.enable_retrieval:
            tools.append({"type": "retrieval"})

        if self.spec.enable_code_interpreter:
            tools.append({"type": "code_interpreter"})

        if len(tools) > 0:
            request["tools"] = tools

        assistant = await llm.beta.assistants.create(**request)
        thread = await llm.beta.threads.create()

        await AgentOS.symbolic_memory.insert_one("open_ai_conversations", {
            "process_id": call_context.process_id,
            "thread_id": call_context.thread_id,
            "assistant_id": assistant.id,
            "assistant_thread_id": thread.id
        })

        return assistant, thread.id

    async def get_tools(self, conversation) -> Dict[str, ToolDefType]:
        self.tool_defs = {}
        for logic_unit in self.logic_units:
            self.tool_defs.update(await logic_unit.build_tools(conversation))
        return self.tool_defs

    async def set_boot_messages(
            self,
            call_context: CallContext,
            boot_messages: List[CPUMessageTypes],
            output_format: Dict[str, Any] = None
    ):
        # separate out the system messages from the user messages
        system_message: str = ""
        user_messages = []
        file_ids = []
        for message in boot_messages:
            if message.type == "system":
                system_message += message.prompt + "\n"
            elif message.type == "user":
                user_messages.append(message.prompt)
            elif message.type == "image":
                file_ids.append(await self.processFile(message))
            else:
                raise ValueError(f"Unknown message type {message.type}")

        system_message += f"\nYour response MUST be valid JSON satisfying the following schema:\n{json.dumps(output_format)}."
        system_message += "\nOnly reply with JSON and no other text.\n"

        assistant, thread_id = await self.get_or_create_assistant(call_context, system_message, file_ids)
        llm = self._getLLM()
        for user_message in user_messages:
            await llm.beta.threads.messages.create(thread_id=thread_id, content=user_message, role="user")

    async def schedule_request(
            self,
            call_context: CallContext,
            prompts: List[CPUMessageTypes],
            output_format: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        # separate out the system messages from the user messages
        user_messages = []
        file_ids = []
        for message in prompts:
            if message.type == "user":
                user_messages.append(message.prompt)
            elif message.type == "image":
                file_ids.append(await self.processFile(message))
            else:
                raise ValueError(f"Unknown message type {message.type}")

        assistant, thread_id = await self.get_or_create_assistant(call_context)
        llm = self._getLLM()
        if len(user_messages) == 0:
            user_messages.append("")

        last_message_id = None
        for idx, user_message in enumerate(user_messages):
            request = {
                "thread_id": thread_id,
                "content": user_message,
                "role": "user"
            }
            if idx == len(user_messages) - 1:
                request["file_ids"] = file_ids
            last_message = await llm.beta.threads.messages.create(**request)
            last_message_id = last_message.id

        # start the run
        return await self.run_llm_and_tools(call_context, assistant.id, thread_id, last_message_id)

    async def _get_tools_defs(self, call_context: CallContext):
        conversation = []
        conversation_from_memory = AgentOS.symbolic_memory.find("open_ai_conversation_data", {
            "process_id": call_context.process_id,
            "thread_id": call_context.thread_id,
        })
        async for item in conversation_from_memory:
            conversation.append(LLMMessage.from_dict(item["tool_result"]))

        tool_defs = await self.get_tools(conversation)
        return tool_defs

    async def run_llm_and_tools(self, call_context: CallContext, assistant_id: str, assistant_thread_id: str, last_message_id: str):
        llm = self._getLLM()
        tool_defs = await self._get_tools_defs(call_context)
        tools = []
        for tool_def in tool_defs.values():
            tools.append(ToolAssistantToolsFunction(**{
                "type": "function",
                "function": {
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "parameters": tool_def.parameters
                }
            }))
        if self.spec.enable_retrieval:
            tools.append({"type": "retrieval"})

        if self.spec.enable_code_interpreter:
            tools.append({"type": "code_interpreter"})
        request = {
            "assistant_id": assistant_id,
            "thread_id": assistant_thread_id
        }
        if len(tools) > 0:
            request["tools"] = tools

        run = await llm.beta.threads.runs.create(**request)
        num_iterations = 0
        while num_iterations < self.spec.max_num_function_calls:
            run = await self.run_llm(run.id, assistant_thread_id)
            if run.status == "requires_action":
                results = []

                for tool_call in run.required_action.submit_tool_outputs.tool_calls:
                    print("executing tool " + tool_call.name + " with args " + str(tool_call.arguments))
                    tool_call_id = tool_call.id
                    function_call = tool_call.function
                    arguments = json.loads(function_call.arguments)
                    print("executing tool " + function_call.name + " with args " + str(function_call.arguments))
                    tool_def = tool_defs[function_call.name]
                    tool_result = await tool_def.execute(call_context=call_context, args=arguments)
                    result_as_json_str = self._to_json(tool_result)
                    message = ToolOutput(tool_call_id=tool_call_id, output=result_as_json_str)
                    message_to_store = ToolResponseMessage(tool_call_id=tool_call.tool_call_id, result=result_as_json_str, name=tool_call.name)
                    await AgentOS.symbolic_memory.insert_one("open_ai_conversation_data", {
                        "process_id": call_context.process_id,
                        "thread_id": call_context.thread_id,
                        "assistant_id": assistant_id,
                        "assistant_thread_id": assistant_thread_id,
                        "tool_call_id": tool_call_id,
                        "tool_result": message_to_store.model_dump()
                    })
                    results.append(message)

                run = await llm.beta.threads.runs.submit_tool_outputs(thread_id=assistant_thread_id, run_id=run.id, tool_outputs=results)
                num_iterations += 1
            else:
                messages = await llm.beta.threads.messages.list(thread_id=assistant_thread_id, before=last_message_id)
                first_item: ThreadMessage = None
                async for item in messages:
                    first_item = item
                    break

                content = ""
                for text in first_item.content:
                    if text.type == "image_url":
                        print("UGH!!! We got an image url")
                    else:
                        content += text.text.value + "\n"
                try:
                    # todo -- remove ```json...``` from the content
                    content = content.replace("```json\n", "").replace("```", "")
                    return json.loads(content)
                except json.JSONDecodeError:
                    print("content was " + content)
                    raise

        raise ValueError(f"Exceeded maximum number of function calls {self.spec.max_num_function_calls}")

        pass

    async def run_llm(self, run_id: str, thread_id: str):
        llm = self._getLLM()
        finished_states = ["completed", "requires_action", "cancelled", "failed", "expired"]
        start_time = time.time()
        run = await llm.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)
        while (time.time() - start_time) < self.spec.max_wait_time_secs:
            if run.status in finished_states:
                break
            await asyncio.sleep(self.spec.llm_poll_interval_ms / 1000)
            run = await llm.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run_id)

        if run.status not in finished_states or run.status == "expired":
            raise RuntimeError("Timeout while waiting for LLM to finish")
        elif run.status == "requires_action":
            return run
        elif run.status == "completed":
            return run
        elif run.status == "cancelled":
            raise RuntimeError("LLM run was cancelled")
        else:
            is_rate_limit = run.last_error.code == "rate_limit"
            raise RuntimeError("LLM run failed because " + run.last_error.message + (" (rate limit)" if is_rate_limit else ""))

    async def main_thread(self, process_id: str) -> Thread:
        return Thread(CallContext(process_id=process_id), self)

    async def new_thread(self, process_id) -> Thread:
        return Thread(CallContext(process_id=process_id).derive_call_context(), self)

    async def clone_thread(self, call_context: CallContext) -> Thread:
        pass