import argparse
import sys
import asyncio
from pathlib import Path

from .config import OuroAgentsConfig
from .agent import OuroAgent
from .server import start_server

def main():
    parser = argparse.ArgumentParser(description="Ouro Agents CLI")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # Server command
    server_parser = subparsers.add_parser("serve", help="Start the FastAPI server")
    
    # Run command
    run_parser = subparsers.add_parser("run", help="Run a single task")
    run_parser.add_argument("task", help="The task for the agent to perform")
    
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
