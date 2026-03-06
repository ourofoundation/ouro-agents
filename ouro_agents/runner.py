import argparse
import asyncio
import sys
from uuid import uuid4

from .config import OuroAgentsConfig, RunMode
from .agent import OuroAgent
from .server import start_server


def _run_chat(config_path: str, conversation_id: str | None) -> int:
    config = OuroAgentsConfig.load_from_file(config_path)

    if not conversation_id:
        conversation_id = str(uuid4())

    print("\n--- Chat Mode ---\n")
    print(f"Conversation ID: {conversation_id}")
    print("Type messages and press enter.")
    print("Commands: /exit, /quit, /new, /conversation <id>\n")

    with OuroAgent(config) as agent:
        while True:
            try:
                user_input = input("you> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nExiting chat.")
                return 0

            if not user_input:
                continue

            if user_input in {"/exit", "/quit"}:
                print("Exiting chat.")
                return 0

            if user_input == "/new":
                conversation_id = str(uuid4())
                print(f"Started new conversation: {conversation_id}")
                continue

            if user_input.startswith("/conversation "):
                next_id = user_input.replace("/conversation ", "", 1).strip()
                if not next_id:
                    print("Please provide a conversation id.")
                    continue
                conversation_id = next_id
                print(f"Switched conversation: {conversation_id}")
                continue

            result = asyncio.run(
                agent.run(user_input, conversation_id=conversation_id, mode=RunMode.CHAT, user_id="creator")
            )
            print("\nagent>")
            print(result)
            print()


def main():
    parser = argparse.ArgumentParser(description="Ouro Agents CLI")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Server command
    server_parser = subparsers.add_parser("serve", help="Start the FastAPI server")
    
    # Run command
    run_parser = subparsers.add_parser("run", help="Run a single task")
    run_parser.add_argument("task", help="The task for the agent to perform")

    # Chat command
    chat_parser = subparsers.add_parser("chat", help="Start interactive chat mode")
    chat_parser.add_argument(
        "--conversation-id",
        default=None,
        help="Resume an existing conversation by id",
    )

    # Heartbeat command
    hb_parser = subparsers.add_parser("heartbeat", help="Run a single heartbeat tick")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        start_server(args.config)
    elif args.command == "run":
        config = OuroAgentsConfig.load_from_file(args.config)
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.run(args.task))
        print("\n--- Result ---\n")
        print(result)
    elif args.command == "chat":
        sys.exit(_run_chat(args.config, args.conversation_id))
    elif args.command == "heartbeat":
        config = OuroAgentsConfig.load_from_file(args.config)
        with OuroAgent(config) as agent:
            result = asyncio.run(agent.heartbeat())
        if result:
            print("\n--- Heartbeat Action Taken ---\n")
            print(result)
        else:
            print("\n--- Heartbeat Suppressed (No Action) ---\n")

if __name__ == "__main__":
    main()
