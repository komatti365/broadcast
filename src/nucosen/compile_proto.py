"""
Build helper script to compile Protocol Buffers for Niconico comment server.
"""

import os
import sys
from pathlib import Path
import grpc_tools
from grpc_tools import protoc

def compile_proto():
    current_dir = Path(__file__).parent.resolve()
    src_dir = current_dir.parent
    
    # Locate proto files in integration-docs
    # Fallback to absolute paths if not in standard layout
    workspace_root = src_dir.parent
    proto_root = workspace_root.parent / "integration-docs" / "example" / "nicolive-comment-protobuf"
    
    if not proto_root.exists():
        print(f"Error: proto root not found at {proto_root}", file=sys.stderr)
        sys.exit(1)
        
    print(f"Compiling Protocol Buffers from: {proto_root}")
    print(f"Output directory: {src_dir}")
    
    # Get the well-known protos path from grpc_tools using __file__
    try:
        grpc_tools_dir = Path(grpc_tools.__file__).parent
        well_known_protos = grpc_tools_dir / "_proto"
        if not well_known_protos.exists():
            # Fallback in case directory structure differs
            well_known_protos = None
    except Exception as e:
        print(f"Warning: could not locate grpc_tools well-known protos directory: {e}", file=sys.stderr)
        well_known_protos = None
    
    # Collect all proto files recursively
    proto_files = []
    for root, _, files in os.walk(proto_root):
        for file in files:
            if file.endswith(".proto"):
                full_path = Path(root) / file
                # Use absolute paths to prevent any resolution issues
                proto_files.append(str(full_path))
                
    if not proto_files:
        print("No .proto files found to compile.", file=sys.stderr)
        sys.exit(1)
        
    # Standard protoc arguments
    args = [
        "grpc_tools.protoc",
        f"-I{proto_root}",
    ]
    
    if well_known_protos:
        args.append(f"-I{well_known_protos}")
        
    args.append(f"--python_out={src_dir}")
    args.extend(proto_files)
    
    print(f"Running command: {' '.join(args)}")
    exit_code = protoc.main(args)
    if exit_code != 0:
        print(f"Error: protoc returned non-zero exit code: {exit_code}", file=sys.stderr)
        sys.exit(exit_code)
        
    print("Protocol Buffers compiled successfully.")

if __name__ == "__main__":
    compile_proto()


