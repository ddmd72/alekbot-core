"""TokenTextFastMCP's `read_only_tools` (EXTERNAL_TASK_QUEUE_RFC §5.4, plan Δ3): a tool in the set is listed with
`readOnlyHint`, one outside it with no annotations; without the set every tool is read-only, as the drive app lists
them. Marker texts only."""
from src.composition.token_text_fastmcp import TokenTextFastMCP
from src.domain import drive_connector_contract as c
from src.services.connector_text_service import ConnectorTextService
from tests.support.drive_connector_texts import RESULT_TOKEN, TOOL_TOKEN, TokenStore


async def _app(**kwargs) -> TokenTextFastMCP:
    texts = ConnectorTextService(TokenStore(), tool_token_id=TOOL_TOKEN, result_token_id=RESULT_TOKEN,
                                 required_tool_keys=c.REQUIRED_TOOL_KEYS, required_result_keys=c.REQUIRED_RESULT_KEYS)
    app = TokenTextFastMCP(texts=texts, initial=await texts.texts(), name="probe", **kwargs)

    @app.tool(name="list_folder")
    async def list_folder(folder: str = "") -> str:
        return folder

    @app.tool(name="open_drive_file")
    async def open_drive_file(file_ref: str = "") -> str:
        return file_ref

    return app


async def test_only_the_tools_in_the_set_are_read_only():
    listed = {tool.name: tool for tool in await (await _app(read_only_tools={"list_folder"})).list_tools()}
    assert listed["list_folder"].annotations is not None and listed["list_folder"].annotations.readOnlyHint is True
    assert listed["open_drive_file"].annotations is None


async def test_an_empty_set_marks_no_tool():
    listed = await (await _app(read_only_tools=frozenset())).list_tools()
    assert [tool.annotations for tool in listed] == [None, None]


async def test_without_a_set_every_tool_is_read_only():
    listed = await (await _app()).list_tools()
    assert [tool.annotations.readOnlyHint for tool in listed] == [True, True]
