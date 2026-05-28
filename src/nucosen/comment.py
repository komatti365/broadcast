"""
Niconico live comment watcher for automated requests.
"""

import io
import re
import time
import requests
import threading
import collections
from logging import getLogger

from dwango.nicolive.chat.service.edge.payload_pb2 import ChunkedEntry, ChunkedMessage
from nucosen.sessionCookie import Session
from nucosen.db import RestDbIo

logger = getLogger(__name__)

def read_varint(stream):
    """Read a varint from a byte stream."""
    value = 0
    shift = 0
    while True:
        b = stream.read(1)
        if not b:
            return None
        byte = b[0]
        value |= (byte & 0x7f) << shift
        if not (byte & 0x80):
            break
        shift += 7
        if shift >= 70:
            raise ValueError("Varint is too long")
    return value

def retrieve_messages(uri, decoder_cls, headers=None, cookies=None):
    """Retrieve length-delimited Protobuf messages from a streaming HTTP response."""
    try:
        resp = requests.get(uri, headers=headers, cookies=cookies, stream=True, timeout=30)
        resp.raise_for_status()
        
        buffer = b""
        for chunk in resp.iter_content(chunk_size=4096):
            buffer += chunk
            stream = io.BytesIO(buffer)
            
            last_pos = 0
            while True:
                pos = stream.tell()
                length = read_varint(stream)
                if length is None:
                    # Varint not fully received
                    break
                
                data = stream.read(length)
                if len(data) < length:
                    # Message payload not fully received yet
                    break
                
                try:
                    msg = decoder_cls()
                    msg.ParseFromString(data)
                    yield msg
                except Exception as parse_err:
                    logger.warning(f"Failed to parse Protobuf message: {parse_err}")
                
                last_pos = stream.tell()
            
            buffer = buffer[last_pos:]
            
    except Exception as e:
        logger.warning(f"Error reading message stream ({uri}): {e}")

class CommentWatcher(object):
    """Watch Niconico live comments in a background thread to harvest song requests."""
    
    def __init__(self, session: Session, db: RestDbIo, live_id: str):
        self.session = session
        self.db = db
        self.live_id = live_id
        self.running = False
        self.thread = None
        
        # Deduplication cache for comment message IDs
        self.processed_msg_ids = set()
        self.msg_id_queue = collections.deque()
        
    def start(self):
        """Start the background watcher thread."""
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info(f"Started comment watcher thread for live ID: {self.live_id}")
        
    def stop(self):
        """Stop the watcher thread and wait for it to exit."""
        self.running = False
        if self.thread:
            # We don't join for too long because request connections might hang,
            # but daemon=True will clean it up on process exit anyway.
            self.thread.join(timeout=2.0)
            logger.info("Stopped comment watcher thread.")
            
    def _get_view_url(self) -> str | None:
        """Resolve message server viewUrl from rooms API."""
        try:
            # Step 1: Resolve user ID (required by rooms API)
            user_id = "0"
            url_user = "https://live2.nicovideo.jp/unama/tool/v2/onairs/user"
            header = {
                "X-niconico-session": self.session.getSessionString(),
                "User-Agent": self.session.user_agent,
            }
            resp_user = requests.get(url_user, headers=header, cookies=self.session.cookie, timeout=10)
            if resp_user.status_code == 200:
                user_id = str(resp_user.json().get("data", {}).get("userId", "0"))
                
            # Step 2: Request rooms endpoint
            url_rooms = f"https://api.live2.nicovideo.jp/api/v1/unama/programs/rooms?userId={user_id}&nicoliveProgramId={self.live_id}"
            token = self.session.getSessionString() # fallback to user_session as Bearer token
            headers_rooms = {
                "User-Agent": self.session.user_agent,
            }
            if token:
                headers_rooms["Authorization"] = f"Bearer {token}"
                
            resp_rooms = requests.get(url_rooms, headers=headers_rooms, cookies=self.session.cookie, timeout=15)
            resp_rooms.raise_for_status()
            
            rooms_data = resp_rooms.json().get("data", [])
            if rooms_data:
                view_url = rooms_data[0].get("viewUrl")
                logger.info(f"Resolved rooms viewUrl: {view_url}")
                return view_url
        except Exception as e:
            logger.error(f"Failed to resolve viewUrl for live room: {e}")
        return None

    def _run(self):
        """Main connection and message retrieval loop."""
        view_url = self._get_view_url()
        if not view_url:
            logger.error("Could not obtain comment server viewUrl. Comment watching aborted.")
            return
            
        at = "now"
        headers = {
            "User-Agent": self.session.user_agent,
        }
        
        while self.running:
            try:
                playlist_uri = f"{view_url}?at={at}"
                logger.debug(f"Fetching comment playlist: {playlist_uri}")
                
                for entry in retrieve_messages(playlist_uri, ChunkedEntry, headers=headers, cookies=self.session.cookie):
                    if not self.running:
                        break
                        
                    if entry.HasField("backward"):
                        # Read backward snapshot for initial states (optional, but good for completeness)
                        snapshot_uri = entry.backward.snapshot.uri
                        self._pull_messages(snapshot_uri, headers)
                        
                    elif entry.HasField("previous"):
                        # Read previous messages segment
                        previous_uri = entry.previous.uri
                        self._pull_messages(previous_uri, headers)
                        
                    elif entry.HasField("segment"):
                        # Read current messages segment
                        segment_uri = entry.segment.uri
                        self._pull_messages(segment_uri, headers)
                        
                    elif entry.HasField("next"):
                        # Update index pointer for next batch of segments
                        at = str(entry.next.at)
                        
                time.sleep(1)
            except Exception as e:
                logger.warning(f"Error in comment watcher loop (reconnecting in 5s): {e}")
                time.sleep(5)

    def _pull_messages(self, segment_uri: str, headers: dict):
        """Pull and parse messages from a specific segment URI."""
        try:
            for msg in retrieve_messages(segment_uri, ChunkedMessage, headers=headers, cookies=self.session.cookie):
                if not self.running:
                    break
                    
                # Message deduplication using meta ID
                msg_id = msg.meta.id if msg.HasField("meta") else None
                if msg_id:
                    if msg_id in self.processed_msg_ids:
                        continue
                    self.processed_msg_ids.add(msg_id)
                    self.msg_id_queue.append(msg_id)
                    
                    # Evict old IDs to prevent memory growth
                    if len(self.msg_id_queue) > 1000:
                        oldest = self.msg_id_queue.popleft()
                        self.processed_msg_ids.discard(oldest)
                
                # Check for chat payload
                if msg.HasField("message") and msg.message.HasField("chat"):
                    chat = msg.message.chat
                    content = chat.content
                    if content:
                        self._process_comment(content)
        except Exception as e:
            logger.debug(f"Error pulling messages from segment: {e}")

    def _process_comment(self, content: str):
        """Harvest song request video IDs from chat content."""
        # Match standard video IDs: sm\d+, nm\d+, so\d+ (case insensitive)
        matches = re.findall(r'(?i)(sm\d+|nm\d+|so\d+)', content)
        for video_id in matches:
            video_id_lower = video_id.lower()
            logger.info(f"Comment Harvested Request: {video_id_lower} (from chat: '{content}')")
            self.db.addRequest(video_id_lower)
