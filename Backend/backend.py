"""
Cambio Card Game Backend
FastAPI backend with WebSocket support for real-time multiplayer gameplay
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from typing import List, Dict, Optional, Set
from datetime import datetime
import uuid
import random
import json
import os
import asyncio
from enum import Enum

# ============================================================================
# Background cleanup task (defined after room_manager is instantiated below)
# ============================================================================
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    """Start background room-cleanup task on startup; cancel on shutdown."""
    async def _cleanup_loop():
        while True:
            await asyncio.sleep(10 * 60)  # Run every 10 minutes
            try:
                room_manager.cleanup_stale_rooms()
            except Exception as e:
                print(f"[Cleanup] Error during cleanup: {e}")

    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

# Initialize FastAPI app
app = FastAPI(title="Cambio Card Game API", lifespan=lifespan)

# Define allowed origins
default_origins = [
    "https://cambiogame.com",
    "https://www.cambiogame.com",
    "https://cambio-webs.onrender.com",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

# Allow additional origins via environment variable
env_origins = os.environ.get("ALLOWED_ORIGINS", "")
if env_origins:
    default_origins.extend([origin.strip() for origin in env_origins.split(",") if origin.strip()])

# CORS middleware for frontend connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=default_origins,  # In production, specify your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (bridge.js, etc.)
# Serve static files (bridge.js, etc.)
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
frontend_dir = os.path.join(base_dir, "Frontend")
if not os.path.exists(frontend_dir):
    # Fallback if running from a different context
    frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../Frontend")

# Explicit route for rules.pdf to ensure it's served correctly
@app.get("/static/rules.pdf")
async def get_rules_pdf():
    pdf_path = os.path.join(frontend_dir, "rules.pdf")
    if os.path.exists(pdf_path):
        return FileResponse(pdf_path, media_type="application/pdf")
    return {"error": "File not found", "path": pdf_path}

@app.get("/bridge.js")
async def get_bridge_js():
    js_path = os.path.join(frontend_dir, "bridge.js")
    if os.path.exists(js_path):
        return FileResponse(js_path, media_type="application/javascript")
    return {"error": "File not found"}

app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

# ============================================================================
# Data Models (Pydantic)
# ============================================================================
class GameStatus(str, Enum):
    WAITING = "waiting"
    PLAYING = "playing"
    FINISHED = "finished"

class Card(BaseModel):
    suit: str
    rank: str
    
    def __str__(self):
        return f"{self.rank} of {self.suit}"

class Player(BaseModel):
    player_id: str
    username: str
    hand: List[Optional[Card]] = []
    score: int = 0
    is_connected: bool = False
    last_draw_source: Optional[str] = None  # deck or discard
    last_drawn_card: Optional[Card] = None
    pending_drawn_card: Optional[Card] = None  # card drawn, awaiting swap or discard choice
    pending_ability: Optional[str] = None  # Ability waiting to be used or skipped
    pending_swap_targets: Optional[Dict] = None # Stores targets for look_and_swap decision phase

class GameState(BaseModel):
    current_turn: Optional[str] = None  # player_id
    deck: List[Card] = []
    discard_pile: List[Card] = []
    game_phase: str = "waiting"  # waiting, dealing, playing, finished
    turn_number: int = 0
    viewing_phase: bool = False
    revealed_cards: Dict[str, List[Card]] = {}  # player_id -> cards they've revealed
    cambio_called: bool = False
    cambio_caller: Optional[str] = None
    final_round_turns: Optional[int] = None

class Room(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    
    room_id: str
    players: List[Player] = []
    game_state: GameState
    status: GameStatus = GameStatus.WAITING
    created_at: datetime
    last_activity: datetime = None  # Updated on any player action
    max_players: int = 4
    min_players: int = 2
    num_decks: int = 1  # Number of decks to use (auto-calculated if >5 players)
    initial_hand_size: int = 4  # Number of cards to deal per player
    red_king_variant: bool = False # If True, Red Kings are -2
    last_winner_id: Optional[str] = None

class CreateRoomRequest(BaseModel):
    username: str
    max_players: int = 4
    num_decks: Optional[int] = None  # If None, auto-calculate based on player count (>5 = 2 decks)
    initial_hand_size: int = 4  # 4, 6, or 8
    red_king_variant: bool = False

class JoinRoomRequest(BaseModel):
    username: str

class WebSocketMessage(BaseModel):
    type: str  # join, play_card, draw_card, reveal_card, game_state_request
    data: Optional[Dict] = None

def get_card_value(card: Card, red_king_variant: bool = False) -> int:
    """Return the scoring value for a card according to Cambio rules."""
    if card.rank == "Ace":
        return 1
    if card.rank in [str(n) for n in range(2, 11)]:
        return int(card.rank)
    if card.rank in ["Jack", "Queen", "King"]:
        # Red kings count as -1 (or -2 if variant active), black kings count as 10
        if card.rank == "King" and card.suit in {"Hearts", "Diamonds"}:
            return -2 if red_king_variant else -1
        return 10
    if card.rank == "Joker":
        return 0
    return 10

def get_card_ability(card: Card) -> Optional[str]:
    """Map a card rank to its special ability."""
    if card.rank in {"7", "8"}:
        return "peek_self"
    if card.rank in {"9", "10"}:
        return "peek_other"
    if card.rank in {"Jack", "Queen"}:
        return "blind_swap"
    if card.rank == "King" and card.suit in {"Clubs", "Spades"}:
        return "look_and_swap"
    return None

# ============================================================================
# Game Room Manager
# ============================================================================

class GameRoomManager:
    def __init__(self):
        self.rooms: Dict[str, Room] = {}
        self.room_connections: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> {player_id -> websocket}
    
    def create_room(self, username: str, max_players: int = 8, num_decks: Optional[int] = None, initial_hand_size: int = 4, red_king_variant: bool = False) -> Room:
        """Create a new game room"""
        room_id = str(uuid.uuid4())[:8]
        player_id = str(uuid.uuid4())[:8]
        
        # Auto-calculate number of decks if not specified: 2 decks if max_players > 5
        if num_decks is None:
            num_decks = 2 if max_players > 5 else 1
        
        player = Player(
            player_id=player_id,
            username=username,
            is_connected=True
        )
        
        room = Room(
            room_id=room_id,
            players=[player],
            game_state=GameState(),
            status=GameStatus.WAITING,
            created_at=datetime.now(),
            last_activity=datetime.now(),
            max_players=max_players,
            num_decks=num_decks,
            initial_hand_size=initial_hand_size,
            red_king_variant=red_king_variant
        )
        
        self.rooms[room_id] = room
        self.room_connections[room_id] = {}
        
        return room
    
    def join_room(self, room_id: str, username: str) -> tuple[Room, str]:
        """Join an existing room, returns (room, player_id)"""
        if room_id not in self.rooms:
            raise HTTPException(status_code=404, detail="Room not found")
        
        room = self.rooms[room_id]
        
        if room.status != GameStatus.WAITING:
            raise HTTPException(status_code=400, detail="Game already started")
        
        if len(room.players) >= room.max_players:
            raise HTTPException(status_code=400, detail="Room is full")
        
        player_id = str(uuid.uuid4())[:8]
        player = Player(
            player_id=player_id,
            username=username,
            is_connected=True
        )
        
        room.players.append(player)
        
        # Removed auto-start - game must be manually started
        return room, player_id
    
    def start_game(self, room_id: str):
        """Start the game in a room"""
        if room_id not in self.rooms:
            return
        
        room = self.rooms[room_id]
        if room.status != GameStatus.WAITING:
            return
        
        room.status = GameStatus.PLAYING
        room.game_state.game_phase = "dealing"
        
        # Auto-adjust number of decks based on actual player count if needed
        # Logic: If cards drawn (players * hand_size) > half a deck (26), add another deck.
        total_drawn = len(room.players) * room.initial_hand_size
        if total_drawn > 26 and room.num_decks == 1:
            room.num_decks = 2
        
        # Safety Check: If user forced 1 deck but we physically need more (e.g. 50 cards needed), force 2.
        # Standard deck = 54 cards. 
        if total_drawn > 48 and room.num_decks == 1:
             room.num_decks = 2
        
        # Create and shuffle deck(s)
        deck = self.create_deck(room.num_decks)
        random.shuffle(deck)
        room.game_state.deck = deck
        
        # Deal cards to players (4 cards for Cambio base rules)
        cards_per_player = room.initial_hand_size
        for player in room.players:
            player.hand = [room.game_state.deck.pop() for _ in range(cards_per_player)]
            player.last_draw_source = None
            player.last_drawn_card = None
            player.pending_drawn_card = None
            player.pending_ability = None

        # Flip the first discard to allow immediate eliminations
        if room.game_state.deck:
            starter_card = room.game_state.deck.pop()
            room.game_state.discard_pile.append(starter_card)
        
        # Start in viewing phase - Players look at 2 cards
        room.game_state.game_phase = "viewing"
        room.game_state.viewing_phase = True
        
        # Set starting player for the round
        starter_id = None
        if room.last_winner_id:
            # Check if winner is still in the room
            if any(p.player_id == room.last_winner_id for p in room.players):
                starter_id = room.last_winner_id
        
        if not starter_id:
            # First round or winner left -> Random player
            if room.players:
                starter_id = random.choice(room.players).player_id
        
        room.game_state.current_turn = starter_id
        room.game_state.turn_number = 1
    
    def create_deck(self, num_decks: int = 1) -> List[Card]:
        """Create one or more standard 54-card decks (52 cards + 2 Jokers per deck)"""
        suits = ["Hearts", "Diamonds", "Clubs", "Spades"]
        ranks = ["Ace", "2", "3", "4", "5", "6", "7", "8", "9", "10", "Jack", "Queen", "King"]
        
        deck = []
        # Create the specified number of decks
        for _ in range(num_decks):
            # Add standard 52 cards
            for suit in suits:
                for rank in ranks:
                    deck.append(Card(suit=suit, rank=rank))
            # Add 2 Jokers per deck
            deck.append(Card(suit="Joker", rank="Joker"))
            deck.append(Card(suit="Joker", rank="Joker"))
        
        return deck
    
    def reshuffle_deck(self, room_id: str):
        """Reshuffle the discard pile (except the last card) back into the deck"""
        if room_id not in self.rooms:
            return
        
        room = self.rooms[room_id]
        
        if len(room.game_state.discard_pile) <= 1:
            # Not enough cards to reshuffle
            return
        
        # Keep the last card in discard pile
        last_card = room.game_state.discard_pile[-1]
        
        # Take all cards except the last one
        cards_to_reshuffle = room.game_state.discard_pile[:-1]
        
        # Clear discard pile and add back only the last card
        room.game_state.discard_pile = [last_card]
        
        # Shuffle the cards and make them the new deck
        random.shuffle(cards_to_reshuffle)
        room.game_state.deck = cards_to_reshuffle
    
    def get_room(self, room_id: str) -> Optional[Room]:
        """Get room by ID"""
        return self.rooms.get(room_id)
    
    def add_connection(self, room_id: str, player_id: str, websocket: WebSocket):
        """Add WebSocket connection for a player"""
        if room_id not in self.room_connections:
            self.room_connections[room_id] = {}
        self.room_connections[room_id][player_id] = websocket
    
    def remove_connection(self, room_id: str, player_id: str):
        """Remove WebSocket connection for a player"""
        if room_id in self.room_connections:
            self.room_connections[room_id].pop(player_id, None)

    def touch_room(self, room_id: str):
        """Update last_activity timestamp for a room."""
        room = self.rooms.get(room_id)
        if room:
            room.last_activity = datetime.now()

    def cleanup_stale_rooms(self):
        """
        Delete rooms that have been inactive too long.
        Thresholds:
          - WAITING rooms with no connected players: 30 minutes
          - WAITING rooms with connected players:    2 hours
          - PLAYING rooms with no connected players: 1 hour
          - FINISHED rooms:                          15 minutes
        """
        now = datetime.now()
        to_delete = []

        for room_id, room in self.rooms.items():
            age = (now - (room.last_activity or room.created_at)).total_seconds()
            connected = sum(1 for p in room.players if p.is_connected)

            if room.status == GameStatus.FINISHED and age > 15 * 60:
                to_delete.append(room_id)
            elif room.status == GameStatus.WAITING and connected == 0 and age > 30 * 60:
                to_delete.append(room_id)
            elif room.status == GameStatus.WAITING and age > 2 * 60 * 60:
                to_delete.append(room_id)
            elif room.status == GameStatus.PLAYING and connected == 0 and age > 60 * 60:
                to_delete.append(room_id)

        for room_id in to_delete:
            self.rooms.pop(room_id, None)
            self.room_connections.pop(room_id, None)
            print(f"[Cleanup] Deleted stale room {room_id}")

        if to_delete:
            print(f"[Cleanup] Removed {len(to_delete)} stale room(s). Active rooms: {len(self.rooms)}")
    
    async def broadcast_to_room(self, room_id: str, message: dict, exclude_player: Optional[str] = None):
        """Broadcast message to all players in a room"""
        if room_id not in self.room_connections:
            return
        
        connections = self.room_connections[room_id]
        
        async def send_to_player(player_id, websocket):
            try:
                await websocket.send_json(message)
                return None
            except Exception as e:
                print(f"Error sending message to player {player_id}: {e}")
                return player_id

        # Create tasks for all players in the room, excluding the specified player if any
        tasks = [send_to_player(pid, ws) for pid, ws in connections.items() if pid != exclude_player]
        
        if not tasks:
            return

        # Run all sends concurrently
        results = await asyncio.gather(*tasks)
        
        # Clean up disconnected players
        for player_id in results:
            if player_id is not None:
                self.remove_connection(room_id, player_id)

    async def send_to_player(self, room_id: str, player_id: str, message: dict):
        """Send a private websocket message to a single player."""
        websocket = self.room_connections.get(room_id, {}).get(player_id)
        if websocket:
            try:
                await websocket.send_json(message)
            except Exception:
                self.remove_connection(room_id, player_id)
    
    def check_win_condition(self, room_id: str) -> Optional[str]:
        """Check if any player has won (empty hand, all None). Returns winner player_id or None"""
        if room_id not in self.rooms:
            return None
        
        room = self.rooms[room_id]
        for player in room.players:
            # Check if all cards are None (eliminated)
            if not any(card for card in player.hand):
                return player.player_id
        return None
    
    def end_game(self, room_id: str, winner_id: str):
        """End the game and set winner"""
        if room_id not in self.rooms:
            return
        
        room = self.rooms[room_id]
        room.status = GameStatus.FINISHED
        room.game_state.game_phase = "finished"
        
        # Store winner ID for next round
        room.last_winner_id = winner_id
    
    def next_turn(self, room_id: str) -> Optional[str]:
        """Move to next player's turn. Returns winner_id if the round ends."""
        if room_id not in self.rooms:
            return None
        
        room = self.rooms[room_id]
        if not room.players:
            return None
        
        current_index = None
        for i, player in enumerate(room.players):
            if player.player_id == room.game_state.current_turn:
                current_index = i
                break
        
        if current_index is not None:
            next_index = (current_index + 1) % len(room.players)
            room.game_state.current_turn = room.players[next_index].player_id
            room.game_state.turn_number += 1
            
            if room.game_state.cambio_called:
                if room.game_state.final_round_turns is None:
                    room.game_state.final_round_turns = len(room.players) - 1
                else:
                    room.game_state.final_round_turns -= 1
                    if room.game_state.final_round_turns <= 0:
                        return self.finish_round(room_id)
        return None

    def finish_round(self, room_id: str) -> Optional[str]:
        """Compute scores and finish the game when Cambio ends."""
        room = self.rooms.get(room_id)
        if not room:
            return None
        
        # Calculate scores
        for player in room.players:
            player.score = sum(get_card_value(card, room.red_king_variant) for card in player.hand if card)
        
        # Determine winner: Lowest score, tie-breaker: fewest cards
        # Sort players by score (asc), then by hand size (asc)
        sorted_players = sorted(
            room.players, 
            key=lambda p: (p.score, len([c for c in p.hand if c]), 0 if p.player_id == room.game_state.cambio_caller else 1)
        )
        
        winner = sorted_players[0] if sorted_players else None
        winner_id = winner.player_id if winner else None
        
        if winner_id:
            self.end_game(room_id, winner_id)
        return winner_id

    async def resolve_card_ability(self, room: Room, acting_player: Player, ability: str, payload: Dict) -> bool:
        """Execute the requested ability if the payload is valid."""
        room_id = room.room_id

        def validate_index(hand: List[Optional[Card]], idx: int) -> bool:
            return 0 <= idx < len(hand) and hand[idx] is not None

        if ability == "peek_self":
            index = payload.get("card_index")
            if index is None or not validate_index(acting_player.hand, index):
                return False
            card = acting_player.hand[index]

            # Send private reveal
            await self.send_to_player(room_id, acting_player.player_id, {
                "type": "ability_resolution",
                "data": {
                    "ability": ability,
                    "card": card.model_dump(mode='json'),
                    "target_player_id": acting_player.player_id,
                    "card_index": index,
                    "duration": 3000
                }
            })

            # Broadcast indicator
            await self.broadcast_to_room(room_id, {
                "type": "card_being_looked_at",
                "data": {
                    "player_id": acting_player.player_id,
                    "target_player_id": acting_player.player_id,
                    "card_index": index,
                    "duration": 3000
                }
            })
            return True

        if ability == "peek_other":
            target_id = payload.get("target_player_id")
            index = payload.get("card_index")
            if not target_id or index is None:
                return False
            target = next((p for p in room.players if p.player_id == target_id), None)
            if not target or not validate_index(target.hand, index):
                return False
            card = target.hand[index]

            # Send private reveal
            await self.send_to_player(room_id, acting_player.player_id, {
                "type": "ability_resolution",
                "data": {
                    "ability": ability,
                    "card": card.model_dump(mode='json'),
                    "target_player_id": target_id,
                    "card_index": index,
                    "duration": 3000
                }
            })

            # Broadcast indicator
            await self.broadcast_to_room(room_id, {
                "type": "card_being_looked_at",
                "data": {
                    "player_id": acting_player.player_id,
                    "target_player_id": target_id,
                    "card_index": index,
                    "duration": 3000
                }
            })
            return True

        if ability == "blind_swap":
            # New Universal Logic: Can swap ANY two cards (source and target)
            # The payload should now contain source/target player/card indices.
            # For backward compatibility or simplicity, we check if new format is used.
            # If "own_card_index" is present, it's the old format (Self <-> Other).
            # But the user specifically requested "any two cards".
            # Let's support a generalized format: "source" and "target" dicts.
            
            source_pid = payload.get("source_player_id")
            source_idx = payload.get("source_card_index")
            target_pid = payload.get("target_player_id")
            target_idx = payload.get("target_card_index")
            
            # Fallback for old frontend logic if not updated yet (though we will update frontend)
            if source_pid is None:
                source_pid = acting_player.player_id
                source_idx = payload.get("own_card_index")
            
            if source_pid is None or source_idx is None or target_pid is None or target_idx is None:
                return False

            # Immunity Check: Cannot swap with a player who called Cambio
            if room.game_state.cambio_caller:
                if room.game_state.cambio_caller == source_pid or room.game_state.cambio_caller == target_pid:
                    # Notify only the acting player? Or return false?
                    # Ideally notify.
                    await self.send_to_player(room_id, acting_player.player_id, {
                        "type": "error",
                        "message": "Cannot swap with a player who called Cambio!"
                    })
                    return False

            source_p = next((p for p in room.players if p.player_id == source_pid), None)
            target_p = next((p for p in room.players if p.player_id == target_pid), None)
            
            if not source_p or not target_p:
                return False
            
            if not validate_index(source_p.hand, source_idx) or not validate_index(target_p.hand, target_idx):
                return False
                
            # Execute Swap
            source_p.hand[source_idx], target_p.hand[target_idx] = target_p.hand[target_idx], source_p.hand[source_idx]
            
            await self.broadcast_to_room(room_id, {
                "type": "cards_swapped",
                "data": {
                    "message": f"{acting_player.username} blind swapped {source_p.username}'s card #{source_idx + 1} with {target_p.username}'s card #{target_idx + 1}.",
                    "player1_id": source_p.player_id,
                    "card1_index": source_idx,
                    "player2_id": target_p.player_id,
                    "card2_index": target_idx,
                    "room": room.model_dump(mode='json')
                }
            })
            return True

        if ability == "look_and_swap":
            # Phase 1: Request to Look
            first = payload.get("first_target")
            second = payload.get("second_target")
            
            def resolve_target(target_payload):
                pid = target_payload.get("player_id")
                idx = target_payload.get("card_index")
                player_obj = next((p for p in room.players if p.player_id == pid), None)
                if player_obj is None or idx is None or not validate_index(player_obj.hand, idx):
                    return None, None
                return player_obj, idx

            first_player, first_idx = resolve_target(first)
            second_player, second_idx = resolve_target(second)
            if not first_player or not second_player:
                return False

            # Immunity Check
            if room.game_state.cambio_caller:
                if room.game_state.cambio_caller == first_player.player_id or room.game_state.cambio_caller == second_player.player_id:
                    await self.send_to_player(room_id, acting_player.player_id, {
                        "type": "error",
                        "message": "Cannot swap with a player who called Cambio!"
                    })
                    return False

            first_card = first_player.hand[first_idx]
            second_card = second_player.hand[second_idx]

            # Store targets for the decision phase
            acting_player.pending_swap_targets = {
                "first_player_id": first_player.player_id,
                "first_card_index": first_idx,
                "second_player_id": second_player.player_id,
                "second_card_index": second_idx
            }
            acting_player.pending_ability = "swap_decision" # New state

            # Reveal the cards to the acting player
            await self.send_to_player(room_id, acting_player.player_id, {
                "type": "ability_resolution",
                "data": {
                    "ability": "look_and_swap",
                    "first": {"player_id": first_player.player_id, "card_index": first_idx, "card": first_card.model_dump(mode='json')},
                    "second": {"player_id": second_player.player_id, "card_index": second_idx, "card": second_card.model_dump(mode='json')},
                    "message": "Review the cards. Do you want to swap them?"
                }
            })

            # Broadcast indicator for FIRST card
            await self.broadcast_to_room(room_id, {
                "type": "card_being_looked_at",
                "data": {
                    "player_id": acting_player.player_id,
                    "target_player_id": first_player.player_id,
                    "card_index": first_idx,
                    "duration": 5000
                }
            })

            # Broadcast indicator for SECOND card
            await self.broadcast_to_room(room_id, {
                "type": "card_being_looked_at",
                "data": {
                    "player_id": acting_player.player_id,
                    "target_player_id": second_player.player_id,
                    "card_index": second_idx,
                    "duration": 5000
                }
            })
            
            # Return True to indicate the *Look* action was successful, but we don't end turn yet
            # because pending_ability is now set to 'swap_decision'
            return True

        return False

def get_room_dict_for_broadcast(room: Room, hide_pending_for_player: Optional[str] = None) -> dict:
    """Get room as dict for broadcasting. Optionally hide pending_drawn_card for a player (so others don't see their drawn card)."""
    d = room.model_dump(mode='json')
    if hide_pending_for_player:
        for p in d.get('players', []):
            if p.get('player_id') == hide_pending_for_player:
                p['pending_drawn_card'] = None
                break
    return d

# Global room manager instance
room_manager = GameRoomManager()

# ============================================================================
# REST API Endpoints
# ============================================================================

@app.get("/")
async def root():
    """Serve the main HTML file"""
    html_path = os.path.join(frontend_dir, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return {"message": "Cambio Card Game API", "status": "running", "note": f"index.html not found in {frontend_dir}"}

@app.get("/instructions")
async def instructions():
    """Serve the instructions HTML file"""
    html_path = os.path.join(frontend_dir, "instructions.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return {"message": "Instructions not found", "status": "running"}

@app.post("/api/rooms", response_model=Room)
async def create_room(request: CreateRoomRequest):
    """Create a new game room"""
    room = room_manager.create_room(
        request.username, 
        request.max_players, 
        request.num_decks,
        request.initial_hand_size,
        request.red_king_variant
    )
    return room

@app.get("/api/rooms/{room_id}", response_model=Room)
async def get_room(room_id: str):
    """Get room status"""
    room = room_manager.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room

@app.post("/api/rooms/{room_id}/join")
async def join_room(room_id: str, request: JoinRoomRequest):
    """Join a room"""
    try:
        room, player_id = room_manager.join_room(room_id, request.username)
        room_manager.touch_room(room_id)
        return {
            "room": room,
            "player_id": player_id,
            "websocket_url": f"/ws/{room_id}"
        }
    except HTTPException as e:
        raise e

@app.post("/api/rooms/{room_id}/start")
async def start_room_game(room_id: str):
    """Manually start the game in a room"""
    room = room_manager.get_room(room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    
    if room.status != GameStatus.WAITING:
        raise HTTPException(status_code=400, detail="Game already started or finished")
    
    if len(room.players) < room.min_players:
        raise HTTPException(status_code=400, detail=f"Need at least {room.min_players} players to start. Currently {len(room.players)} player(s).")
    
    room_manager.start_game(room_id)
    room = room_manager.get_room(room_id)
    
    # Broadcast game started event
    await room_manager.broadcast_to_room(room_id, {
        "type": "game_started",
        "data": {
            "room": room.model_dump(mode='json')
        }
    })
    
    return {"room": room, "message": "Game started successfully"}

# ============================================================================
# WebSocket Handler
# ============================================================================

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    """WebSocket endpoint for real-time game communication"""
    await websocket.accept()
    
    player_id = None
    room = None
    
    try:
        # Wait for initial join message
        data = await websocket.receive_text()
        message = json.loads(data)
        
        if message.get("type") != "join":
            await websocket.send_json({
                "type": "error",
                "message": "First message must be 'join' with player_id"
            })
            await websocket.close()
            return
        
        player_id = message.get("data", {}).get("player_id")
        if not player_id:
            await websocket.send_json({
                "type": "error",
                "message": "player_id required in join message"
            })
            await websocket.close()
            return
        
        room = room_manager.get_room(room_id)
        if not room:
            await websocket.send_json({
                "type": "error",
                "message": "Room not found"
            })
            await websocket.close()
            return
        
        # Verify player is in room
        player = next((p for p in room.players if p.player_id == player_id), None)
        if not player:
            await websocket.send_json({
                "type": "error",
                "message": "Player not in room"
            })
            await websocket.close()
            return
        
        # Add connection
        room_manager.add_connection(room_id, player_id, websocket)
        player.is_connected = True
        
        # Send current game state
        await websocket.send_json({
            "type": "game_state",
            "data": {
                "room": room.model_dump(mode='json'),
                "your_player_id": player_id
            }
        })
        
        # Notify other players with updated room state
        room = room_manager.get_room(room_id)
        await room_manager.broadcast_to_room(room_id, {
            "type": "player_joined",
            "data": {
                "player_id": player_id,
                "username": player.username,
                "room": room.model_dump(mode='json')
            }
        }, exclude_player=player_id)
        
        # Main message loop
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type")
            
            room = room_manager.get_room(room_id)
            if not room:
                break
            
            player = next((p for p in room.players if p.player_id == player_id), None)
            if not player:
                break

            # Update last_activity on every player action
            room_manager.touch_room(room_id)
            
            if msg_type == "play_card":
                # Play a card from hand (Elimination/Sacrifice)
                # This corresponds to "matching one of your cards with the one on the top of the discard pile"
                card_data = message.get("data", {}).get("card")
                card_index = message.get("data", {}).get("card_index")

                if not card_data:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Card data required"
                    })
                    continue
                
                card = Card(**card_data)
                
                # Check if game is still active
                if room.status != GameStatus.PLAYING:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Game is not active"
                    })
                    continue
                
                # Elimination can be done by anyone at any time (except during viewing phase probably)
                if room.game_state.game_phase == "viewing":
                     await websocket.send_json({
                        "type": "error",
                        "message": "Cannot play cards during viewing phase"
                    })
                     continue

                if player.pending_drawn_card:
                    # If you have a drawn card pending, you might still be able to eliminate other cards from your hand?
                    # The rules say "Eliminations can happen at any point".
                    # But it might complicate the UI/state. Let's allow it for now.
                    pass
                
                # Check if player has the card (and it's not None)
                card_found = False
                played_card = None
                hand_index = None

                if card_index is not None:
                    # Use specific index provided by client (fixes duplicate card issues)
                    if 0 <= card_index < len(player.hand) and player.hand[card_index]:
                        hand_card = player.hand[card_index]
                        # Verify it matches the card data sent (optional but good for consistency)
                        if hand_card.suit == card.suit and hand_card.rank == card.rank:
                            played_card = hand_card
                            hand_index = card_index
                            card_found = True
                
                if not card_found:
                    # Fallback to search by value (if index not provided or mismatch)
                    for i, hand_card in enumerate(player.hand):
                        if hand_card and hand_card.suit == card.suit and hand_card.rank == card.rank:
                            played_card = hand_card
                            hand_index = i
                            card_found = True
                            break
                
                if not card_found:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Card not in hand or already eliminated"
                    })
                    continue

                # Sacrifice rule: card must match the top of the discard pile
                top_discard = room.game_state.discard_pile[-1] if room.game_state.discard_pile else None
                if top_discard and played_card.rank != top_discard.rank:
                    # Wrong guess - punishment: draw a card face down
                    # In this version, we will enforce it ends the turn if it WAS your turn?
                    # Or just penalty card. Rules say "Becareful not to incur the penalty".
                    # Usually penalty = draw card. Turn continuation depends on house rules.
                    # Given "Eliminations can happen at any point", it probably shouldn't end turn unless it was your turn action.
                    # But play_card is NOT the turn action (Draw is). So we just give penalty.
                    
                    if not room.game_state.deck:
                        if len(room.game_state.discard_pile) <= 1:
                            await websocket.send_json({
                                "type": "error",
                                "message": "Deck is empty and cannot be reshuffled"
                            })
                            continue
                        room_manager.reshuffle_deck(room_id)
                        room = room_manager.get_room(room_id)
                    
                    if room.game_state.deck:
                        drawn_card = room.game_state.deck.pop()
                        player.hand.append(drawn_card)
                        
                        # Notify player of penalty
                        await websocket.send_json({
                            "type": "wrong_sacrifice_penalty",
                            "data": {
                                "message": "Wrong card! That doesn't match the discard. You drew a penalty card.",
                                "room": room.model_dump(mode='json')
                            }
                        })
                        
                        # Notify others
                        await room_manager.broadcast_to_room(room_id, {
                            "type": "player_penalty_draw",
                            "data": {
                                "player_id": player_id,
                                "message": f"{player.username} played the wrong card and drew a penalty!",
                                "room": room.model_dump(mode='json')
                            }
                        }, exclude_player=player_id)
                    continue

                # Card matches - remove from hand (set to None) and add to discard
                player.hand[hand_index] = None
                room.game_state.discard_pile.append(played_card)

                # Elimination does NOT trigger abilities. "If and only if you draw a card from the deck...".
                
                # Check for win condition (empty hand)
                winner_id = room_manager.check_win_condition(room_id)
                if winner_id:
                    room_manager.end_game(room_id, winner_id)
                    room = room_manager.get_room(room_id)
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "game_ended",
                        "data": {
                            "winner_id": winner_id,
                            "winner_username": next((p.username for p in room.players if p.player_id == winner_id), "Unknown"),
                            "room": room.model_dump(mode='json')
                        }
                    })
                
                # Broadcast update
                room = room_manager.get_room(room_id)
                await room_manager.broadcast_to_room(room_id, {
                    "type": "card_played",
                    "data": {
                        "player_id": player_id,
                        "card": card.model_dump(mode='json'),
                        "room": room.model_dump(mode='json')
                    }
                })
            
            elif msg_type == "draw_card":
                # Draw a card from deck
                # Check if game is still active
                if room.status != GameStatus.PLAYING:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Game is not active"
                    })
                    continue
                
                if room.game_state.current_turn != player_id:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Not your turn"
                    })
                    continue

                if player.pending_drawn_card:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Resolve your drawn card first (swap or discard)"
                    })
                    continue
                
                if player.pending_ability:
                    await websocket.send_json({
                        "type": "error",
                        "message": "You must use or skip your pending ability first"
                    })
                    continue
                
                # If deck is empty, reshuffle discard pile (keeping last card)
                if not room.game_state.deck:
                    if len(room.game_state.discard_pile) <= 1:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Deck is empty and cannot be reshuffled"
                        })
                        continue
                    
                    # Reshuffle the deck
                    room_manager.reshuffle_deck(room_id)
                    room = room_manager.get_room(room_id)
                    
                    # Safety check: ensure deck has cards after reshuffling
                    if not room.game_state.deck:
                        await websocket.send_json({
                            "type": "error",
                            "message": "Failed to reshuffle deck"
                        })
                        continue
                    
                    # Notify all players that deck was reshuffled
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "deck_reshuffled",
                        "data": {
                            "message": "Deck has been reshuffled",
                            "room": room.model_dump(mode='json')
                        }
                    })
                
                drawn_card = room.game_state.deck.pop()
                player.pending_drawn_card = drawn_card
                player.last_draw_source = "deck"
                player.last_drawn_card = drawn_card
                
                # Do NOT add to hand or move to next turn yet
                
                # Send card to player so they can decide
                await websocket.send_json({
                    "type": "card_drawn",
                    "data": {
                        "card": drawn_card.model_dump(mode='json'),
                        "room": room.model_dump(mode='json')
                    }
                })
                
                # Notify others that a card was drawn (but not what it is - hide pending_drawn_card)
                await room_manager.broadcast_to_room(room_id, {
                    "type": "player_drew_card",
                    "data": {
                        "player_id": player_id,
                        "room": get_room_dict_for_broadcast(room, hide_pending_for_player=player_id)
                    }
                }, exclude_player=player_id)

            elif msg_type == "draw_from_discard":
                # Draw a card from discard pile (must swap)
                # Check if game is still active
                if room.status != GameStatus.PLAYING:
                    await websocket.send_json({"type": "error", "message": "Game is not active"})
                    continue
                
                if room.game_state.current_turn != player_id:
                    await websocket.send_json({"type": "error", "message": "Not your turn"})
                    continue

                if player.pending_drawn_card:
                    await websocket.send_json({"type": "error", "message": "Resolve your drawn card first"})
                    continue
                
                if not room.game_state.discard_pile:
                    await websocket.send_json({"type": "error", "message": "Discard pile is empty"})
                    continue

                drawn_card = room.game_state.discard_pile.pop()
                player.pending_drawn_card = drawn_card
                player.last_draw_source = "discard"
                player.last_drawn_card = drawn_card
                
                await websocket.send_json({
                    "type": "card_drawn",
                    "data": {
                        "card": drawn_card.model_dump(mode='json'),
                        "room": room.model_dump(mode='json'),
                        "source": "discard"
                    }
                })
                
                await room_manager.broadcast_to_room(room_id, {
                    "type": "player_drew_card",
                    "data": {
                        "player_id": player_id,
                        "room": get_room_dict_for_broadcast(room, hide_pending_for_player=player_id),
                        "source": "discard"
                    }
                }, exclude_player=player_id)

            elif msg_type == "resolve_draw":
                # Handle the player's choice after drawing: 'swap' or 'discard'
                action = message.get("data", {}).get("action")
                
                if not player.pending_drawn_card:
                    await websocket.send_json({"type": "error", "message": "No pending drawn card"})
                    continue

                if action == "swap":
                    # Swap: exchange drawn card with a card in hand. The hand card goes to discard.
                    # A swap is NOT a discard - no match required. You can swap any card.
                    hand_index = message.get("data", {}).get("card_index")
                    if hand_index is None or hand_index < 0 or hand_index >= len(player.hand):
                        await websocket.send_json({"type": "error", "message": "Invalid hand index"})
                        continue
                    
                    if player.hand[hand_index] is None:
                        await websocket.send_json({"type": "error", "message": "Cannot swap with an empty slot"})
                        continue

                    discarded_card = player.hand[hand_index]
                    # Execute swap - no match required for swap
                    player.hand[hand_index] = player.pending_drawn_card
                    room.game_state.discard_pile.append(discarded_card)
                    player.pending_drawn_card = None
                    
                    cambio_winner = room_manager.next_turn(room_id)
                    room = room_manager.get_room(room_id)
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "cards_swapped",
                        "data": {
                            "message": f"{player.username} swapped their card #{hand_index + 1} with the drawn card.",
                            "player1_id": player.player_id,
                            "card1_index": hand_index,
                            "room": room.model_dump(mode='json')
                        }
                    })
                    winner_id = room_manager.check_win_condition(room_id)
                    if winner_id:
                        room_manager.end_game(room_id, winner_id)
                        room = room_manager.get_room(room_id)
                        await room_manager.broadcast_to_room(room_id, {
                            "type": "game_ended",
                            "data": {"winner_id": winner_id, "winner_username": next((p.username for p in room.players if p.player_id == winner_id), "Unknown"), "room": room.model_dump(mode='json')}
                        })
                    elif cambio_winner:
                        await room_manager.broadcast_to_room(room_id, {
                            "type": "game_ended",
                            "data": {"winner_id": cambio_winner, "winner_username": next((p.username for p in room.players if p.player_id == cambio_winner), "Unknown"), "room": room.model_dump(mode='json')}
                        })

                elif action == "discard":
                    # You can only discard if you drew from the deck
                    if player.last_draw_source == "discard":
                        await websocket.send_json({"type": "error", "message": "You must swap when drawing from discard pile"})
                        continue

                    # Discard the drawn card. No matching required as we are just discarding the card we drew.
                    # "You can then choose to discard the card you drew"
                    card = player.pending_drawn_card
                    
                    room.game_state.discard_pile.append(card)
                    player.pending_drawn_card = None
                    
                    # Check for ability
                    ability_name = get_card_ability(card)
                    if ability_name:
                         # Check if player is "immune" because they called Cambio?
                         # "The person who called Canbio... is immune to any abilities."
                         # But this is the active player using their own ability.
                         
                         player.pending_ability = ability_name
                         # Send ability opportunity
                         await websocket.send_json({
                            "type": "ability_opportunity",
                            "data": {
                                "ability": ability_name,
                                "message": f"You discarded a {card.rank}. You may use its ability: {ability_name}",
                                "room": room.model_dump(mode='json')
                            }
                         })
                         # Turn does not end yet
                    else:
                        # End turn
                        cambio_winner = room_manager.next_turn(room_id)
                        room = room_manager.get_room(room_id)
                        await room_manager.broadcast_to_room(room_id, {
                            "type": "turn_ended",
                            "data": {"room": room.model_dump(mode='json')}
                        })
                        winner_id = room_manager.check_win_condition(room_id)
                        if winner_id:
                            room_manager.end_game(room_id, winner_id)
                            room = room_manager.get_room(room_id)
                            await room_manager.broadcast_to_room(room_id, {
                                "type": "game_ended",
                                "data": {"winner_id": winner_id, "winner_username": next((p.username for p in room.players if p.player_id == winner_id), "Unknown"), "room": room.model_dump(mode='json')}
                            })
                        elif cambio_winner:
                            await room_manager.broadcast_to_room(room_id, {
                                "type": "game_ended",
                                "data": {"winner_id": cambio_winner, "winner_username": next((p.username for p in room.players if p.player_id == cambio_winner), "Unknown"), "room": room.model_dump(mode='json')}
                            })

            elif msg_type == "use_ability":
                if not player.pending_ability:
                    await websocket.send_json({"type": "error", "message": "No pending ability"})
                    continue
                
                payload = message.get("data", {})
                ability_name = player.pending_ability
                
                resolved = await room_manager.resolve_card_ability(room, player, ability_name, payload)
                if resolved:
                    # If the ability moved us to a decision state (like 'swap_decision'), do NOT end turn yet
                    if player.pending_ability == "swap_decision":
                        # Wait for next message 'resolve_swap_decision'
                        pass
                    else:
                        player.pending_ability = None
                        # End turn
                        cambio_winner = room_manager.next_turn(room_id)
                        room = room_manager.get_room(room_id)
                        await room_manager.broadcast_to_room(room_id, {
                            "type": "turn_ended",
                            "data": {"room": room.model_dump(mode='json')}
                        })
                        if cambio_winner:
                            await room_manager.broadcast_to_room(room_id, {
                                "type": "game_ended",
                                "data": {"winner_id": cambio_winner, "winner_username": next((p.username for p in room.players if p.player_id == cambio_winner), "Unknown"), "room": room.model_dump(mode='json')}
                            })
                else:
                    await websocket.send_json({"type": "error", "message": "Invalid ability usage"})

            elif msg_type == "resolve_swap_decision":
                if player.pending_ability != "swap_decision" or not player.pending_swap_targets:
                    await websocket.send_json({"type": "error", "message": "No pending swap decision"})
                    continue
                
                do_swap = message.get("data", {}).get("swap", False)
                targets = player.pending_swap_targets
                
                if do_swap:
                    # Execute swap
                    p1 = next((p for p in room.players if p.player_id == targets["first_player_id"]), None)
                    p2 = next((p for p in room.players if p.player_id == targets["second_player_id"]), None)
                    
                    if p1 and p2:
                        idx1 = targets["first_card_index"]
                        idx2 = targets["second_card_index"]
                        # Validate indices again just in case
                        # Also check for None (though user might have swapped empty slots? Rules usually forbid swapping empty slots.
                        # But if we use None for holes, we probably shouldn't allow selecting holes.
                        # Let's ensure slots are not None.
                        if (0 <= idx1 < len(p1.hand) and p1.hand[idx1] is not None and 
                            0 <= idx2 < len(p2.hand) and p2.hand[idx2] is not None):
                            
                            p1.hand[idx1], p2.hand[idx2] = p2.hand[idx2], p1.hand[idx1]
                            room = room_manager.get_room(room_id)
                            await room_manager.broadcast_to_room(room_id, {
                                "type": "cards_swapped",
                                "data": {
                                    "message": f"{player.username} swapped {p1.username}'s card #{idx1 + 1} with {p2.username}'s card #{idx2 + 1}.",
                                    "player1_id": p1.player_id,
                                    "card1_index": idx1,
                                    "player2_id": p2.player_id,
                                    "card2_index": idx2,
                                    "room": room.model_dump(mode='json')
                                }
                            })
                
                else:
                    room = room_manager.get_room(room_id)
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "decision_notification",
                        "data": {
                            "message": f"{player.username} chose not to swap.",
                            "room": room.model_dump(mode="json")
                        }
                    })

                # Clear state and end turn
                player.pending_ability = None
                player.pending_swap_targets = None
                
                cambio_winner = room_manager.next_turn(room_id)
                room = room_manager.get_room(room_id)
                await room_manager.broadcast_to_room(room_id, {
                    "type": "turn_ended",
                    "data": {"room": room.model_dump(mode='json')}
                })
                if cambio_winner:
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "game_ended",
                        "data": {"winner_id": cambio_winner, "winner_username": next((p.username for p in room.players if p.player_id == cambio_winner), "Unknown"), "room": room.model_dump(mode='json')}
                    })
            
            elif msg_type == "skip_ability":
                if not player.pending_ability:
                    await websocket.send_json({"type": "error", "message": "No pending ability"})
                    continue
                
                player.pending_ability = None
                # End turn
                cambio_winner = room_manager.next_turn(room_id)
                room = room_manager.get_room(room_id)
                await room_manager.broadcast_to_room(room_id, {
                    "type": "turn_ended",
                    "data": {"room": room.model_dump(mode='json')}
                })
                if cambio_winner:
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "game_ended",
                        "data": {"winner_id": cambio_winner, "winner_username": next((p.username for p in room.players if p.player_id == cambio_winner), "Unknown"), "room": room.model_dump(mode='json')}
                    })

            elif msg_type == "end_viewing":
                # Transition from Viewing Phase to Playing Phase
                if room.game_state.game_phase == "viewing":
                    room.game_state.game_phase = "playing"
                    room.game_state.viewing_phase = False
                    
                    # Start the game loop
                    if room.players:
                        # Ensure turn is set (should be set by start_game)
                        if not room.game_state.current_turn:
                            room.game_state.current_turn = room.players[0].player_id
                        room.game_state.turn_number = 1
                    
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "round_started",
                        "data": {"room": room.model_dump(mode='json')}
                    })

            elif msg_type == "start_game":
                # Only allow starting if game is waiting and enough players
                if room.status != GameStatus.WAITING:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Game already started or finished"
                    })
                    continue
                
                if len(room.players) < room.min_players:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Need at least {room.min_players} players to start. Currently {len(room.players)} player(s)."
                    })
                    continue
                
                # Start the game
                room_manager.start_game(room_id)
                room = room_manager.get_room(room_id)
                
                # Broadcast to all players that game started
                await room_manager.broadcast_to_room(room_id, {
                    "type": "game_started",
                    "data": {
                        "room": room.model_dump(mode='json')
                    }
                })
            
            elif msg_type == "call_cambio":
                # Check if it's the player's turn
                if room.game_state.current_turn != player_id:
                    await websocket.send_json({
                        "type": "error",
                        "message": "You can only call Cambio on your turn"
                    })
                    continue

                # Check if they have already drawn a card or have a pending ability (must be start of turn)
                if player.pending_drawn_card or player.pending_ability:
                    await websocket.send_json({
                        "type": "error",
                        "message": "You can only call Cambio at the start of your turn (before drawing)"
                    })
                    continue

                if room.game_state.cambio_called:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Cambio has already been called"
                    })
                    continue
                
                room.game_state.cambio_called = True
                room.game_state.cambio_caller = player_id
                # final_round_turns will be initialized in next_turn()

                await room_manager.broadcast_to_room(room_id, {
                    "type": "cambio_called",
                    "data": {
                        "player_id": player_id,
                        "message": f"{player.username} called Cambio!",
                        "room": room.model_dump(mode='json')
                    }
                })

                # End the turn immediately
                cambio_winner = room_manager.next_turn(room_id)
                room = room_manager.get_room(room_id)
                await room_manager.broadcast_to_room(room_id, {
                    "type": "turn_ended",
                    "data": {"room": room.model_dump(mode='json')}
                })
                if cambio_winner:
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "game_ended",
                        "data": {"winner_id": cambio_winner, "winner_username": next((p.username for p in room.players if p.player_id == cambio_winner), "Unknown"), "room": room.model_dump(mode='json')}
                    })
            
            elif msg_type == "eliminate_card":
                elimination_data = message.get("data", {})
                target_id = elimination_data.get("target_player_id")
                target_index = elimination_data.get("card_index")
                replacement_index = elimination_data.get("replacement_card_index")

                if target_id is None or target_index is None:
                    await websocket.send_json({
                        "type": "error",
                        "message": "target_player_id and card_index are required"
                    })
                    continue

                # Eliminations can happen on any turn - not just yours

                if not room.game_state.discard_pile:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Discard pile is empty"
                    })
                    continue

                target_player = next((p for p in room.players if p.player_id == target_id), None)
                if not target_player:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Target player not found"
                    })
                    continue
                
                # Check replacement card if targeting opponent
                if target_id != player_id:
                    if replacement_index is None:
                        await websocket.send_json({
                            "type": "error",
                            "message": "You must select a card to give to the opponent."
                        })
                        continue
                    if replacement_index < 0 or replacement_index >= len(player.hand):
                         await websocket.send_json({
                            "type": "error",
                            "message": "Invalid replacement card index"
                        })
                         continue

                # Can eliminate anyone's card including your own (e.g. when it's not your turn)

                if target_index < 0 or target_index >= len(target_player.hand) or target_player.hand[target_index] is None:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Invalid card index or empty slot"
                    })
                    continue

                top_card = room.game_state.discard_pile[-1]
                target_card = target_player.hand[target_index]

                if target_card.rank != top_card.rank:
                    # Wrong guess - same penalty as wrong sacrifice: draw card face down, end turn
                    if not room.game_state.deck:
                        if len(room.game_state.discard_pile) <= 1:
                            await websocket.send_json({
                                "type": "error",
                                "message": "Deck is empty and cannot be reshuffled"
                            })
                            continue
                        room_manager.reshuffle_deck(room_id)
                        room = room_manager.get_room(room_id)
                    
                    if room.game_state.deck:
                        drawn_card = room.game_state.deck.pop()
                        player.hand.append(drawn_card)
                        player.last_draw_source = None
                        player.last_drawn_card = None
                        
                        cambio_winner = room_manager.next_turn(room_id)
                        room = room_manager.get_room(room_id)
                        
                        await websocket.send_json({
                            "type": "wrong_sacrifice_penalty",
                            "data": {
                                "message": "Wrong guess! That card doesn't match the discard. You drew a penalty card.",
                                "room": room.model_dump(mode='json')
                            }
                        })
                        
                        await room_manager.broadcast_to_room(room_id, {
                            "type": "player_penalty_draw",
                            "data": {
                                "player_id": player_id,
                                "message": f"{player.username} guessed wrong and drew a penalty!",
                                "room": room.model_dump(mode='json')
                            }
                        }, exclude_player=player_id)
                        
                        if cambio_winner:
                            await room_manager.broadcast_to_room(room_id, {
                                "type": "game_ended",
                                "data": {
                                    "winner_id": cambio_winner,
                                    "winner_username": next((p.username for p in room.players if p.player_id == cambio_winner), "Unknown"),
                                    "room": room.model_dump(mode='json')
                                }
                            })
                    continue

                removed_card = target_player.hand[target_index]
                target_player.hand[target_index] = None # Create hole
                room.game_state.discard_pile.append(removed_card)
                
                msg_extra = ""
                if target_id != player_id:
                     # Move replacement card
                     # Wait, if we use None for holes, popping changes indices. We should set to None.
                     # But rule says: "I give them one of my cards, and that card goes to that bottom left position".
                     # So target slot gets filled. My slot becomes None (or shifted?).
                     # User said: "If I get rid of the top right card, there should just be a hole there". 
                     # But for swapping replacement: "I give them one of my cards, and that card goes to that bottom left position".
                     # This implies target hole is filled immediately.
                     # But my card leaves a hole in MY hand?
                     # "If I get rid of the top right card... hole there". This context was elimination without replacement (self elimination or just hole logic).
                     # Let's assume replacement fills the hole. And the GIVER gets a hole.
                     
                     if player.hand[replacement_index] is None:
                         await websocket.send_json({"type": "error", "message": "Cannot replace with an empty slot"})
                         continue

                     replacement_card = player.hand[replacement_index]
                     player.hand[replacement_index] = None # Giver gets a hole
                     target_player.hand[target_index] = replacement_card # Target hole filled
                     
                     msg_extra = " and gave them a replacement card"
                
                room = room_manager.get_room(room_id)

                # Eliminations don't end your turn - you can do as many as you want
                await room_manager.broadcast_to_room(room_id, {
                    "type": "card_eliminated",
                    "data": {
                        "initiator": player_id,
                        "target_player_id": target_id,
                        "removed_card": removed_card.model_dump(mode='json'),
                        "message": f"{player.username} eliminated {target_player.username}'s card{msg_extra}.",
                        "room": room.model_dump(mode='json')
                    }
                })

                winner_id = room_manager.check_win_condition(room_id)
                if winner_id:
                    room_manager.end_game(room_id, winner_id)
                    room = room_manager.get_room(room_id)
                    await room_manager.broadcast_to_room(room_id, {
                        "type": "game_ended",
                        "data": {
                            "winner_id": winner_id,
                            "winner_username": next((p.username for p in room.players if p.player_id == winner_id), "Unknown"),
                            "room": room.model_dump(mode='json')
                        }
                    })

            elif msg_type == "reveal_card":
                # Reveal a card to other players (memory aspect of Cambio)
                card_data = message.get("data", {}).get("card")
                if not card_data:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Card data required"
                    })
                    continue
                
                card = Card(**card_data)
                
                # Check if player has the card
                if not any(c and c.suit == card.suit and c.rank == card.rank for c in player.hand):
                    await websocket.send_json({
                        "type": "error",
                        "message": "Card not in hand"
                    })
                    continue
                
                # Add to revealed cards
                if player_id not in room.game_state.revealed_cards:
                    room.game_state.revealed_cards[player_id] = []
                room.game_state.revealed_cards[player_id].append(card)
                
                # Broadcast to all players
                await room_manager.broadcast_to_room(room_id, {
                    "type": "card_revealed",
                    "data": {
                        "player_id": player_id,
                        "card": card.model_dump(mode='json'),
                        "room": room.model_dump(mode='json')
                    }
                })
            
            elif msg_type == "game_state_request":
                # Send current game state
                await websocket.send_json({
                    "type": "game_state",
                    "data": {
                        "room": room.model_dump(mode='json'),
                        "your_player_id": player_id
                    }
                })

            elif msg_type == "play_again":
                # Reset game state to waiting
                if room.status != GameStatus.FINISHED:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Game is not finished yet"
                    })
                    continue

                # Reset game state
                room.status = GameStatus.WAITING
                room.game_state = GameState()
                
                # Reset player hands and states (but keep scores? User said "Play Again" usually implies fresh start or round. 
                # Let's keep scores if they want to track rounds, but clear hands.)
                # "it bring us back to the start lobby and shows the players" -> usually implies full reset or new round.
                # Let's reset everything for a fresh game as per "bring us back to the start lobby".
                for p in room.players:
                    p.hand = []
                    p.last_draw_source = None
                    p.last_drawn_card = None
                    p.pending_drawn_card = None
                    p.pending_ability = None
                    p.pending_swap_targets = None
                    # Optional: Reset score if it's a new game? Or keep for session?
                    # "bring us back to the start lobby" sounds like a full reset.
                    # But often friends play multiple rounds. Let's keep scores for now? 
                    # User: "shows the players".
                    # Let's NOT reset scores so they can see who is winning overall.
                
                # Broadcast reset
                await room_manager.broadcast_to_room(room_id, {
                    "type": "game_reset",
                    "data": {
                        "room": room.model_dump(mode='json'),
                        "message": f"{player.username} requested to play again."
                    }
                })
            
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"Unknown message type: {msg_type}"
                })
    
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        # Clean up connection
        if player_id and room_id:
            room_manager.remove_connection(room_id, player_id)
            room = room_manager.get_room(room_id)
            if room:
                player = next((p for p in room.players if p.player_id == player_id), None)
                if player:
                    player.is_connected = False
                
                # Notify other players with updated room state
                room = room_manager.get_room(room_id)
                await room_manager.broadcast_to_room(room_id, {
                    "type": "player_left",
                    "data": {
                        "player_id": player_id,
                        "room": room.model_dump(mode='json')
                    }
                }, exclude_player=player_id)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)