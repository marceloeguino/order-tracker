"""Stand-in for `claude -p`: reads the prompt on stdin, prints stream-json."""
import json
import sys

prompt = sys.stdin.read()
args = " ".join(sys.argv[1:])
text = "Got the test notification.\nSTATUS: TEST_OK" if "TEST notification" in prompt else "Fixed it.\nSTATUS: FIXED"
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}))
print(json.dumps({"type": "result", "subtype": "success", "result": text + f"\n[args: {len(args)}]"}))
