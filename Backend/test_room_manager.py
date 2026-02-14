import pytest
from Backend.backend import GameRoomManager, Room, Player, GameStatus, GameState

def test_deck_auto_adjustment_below_threshold():
    manager = GameRoomManager()

    # Test Case 1: 4 players, 4 cards each (total 16). num_decks should remain 1.
    room = manager.create_room(username="Player1", max_players=4, num_decks=1, initial_hand_size=4)
    # Add 3 more players
    for i in range(2, 5):
        manager.join_room(room.room_id, f"Player{i}")

    manager.start_game(room.room_id)
    assert room.num_decks == 1
    assert len(room.game_state.deck) == 54 - (4 * 4) - 1 # 1 deck (54 cards) - 16 cards - 1 starter card

def test_deck_no_adjustment_at_boundary():
    manager = GameRoomManager()

    # Test Case: Exactly 26 cards drawn (e.g., 2 players, 13 cards each).
    # Logic: total_drawn > 26. So 26 should NOT trigger adjustment.
    room = manager.create_room(username="Player1", max_players=2, num_decks=1, initial_hand_size=13)
    manager.join_room(room.room_id, "Player2")

    manager.start_game(room.room_id)
    assert room.num_decks == 1
    assert len(room.game_state.deck) == 54 - 26 - 1

def test_deck_auto_adjustment_above_threshold():
    manager = GameRoomManager()

    # Test Case 2: 7 players, 4 cards each (total 28). num_decks should auto-adjust to 2.
    room = manager.create_room(username="Player1", max_players=8, num_decks=1, initial_hand_size=4)
    # Add 6 more players
    for i in range(2, 8):
        manager.join_room(room.room_id, f"Player{i}")

    manager.start_game(room.room_id)
    assert room.num_decks == 2
    assert len(room.game_state.deck) == (2 * 54) - (7 * 4) - 1

def test_deck_auto_adjustment_safety_check():
    manager = GameRoomManager()

    # Test Case 3: 10 players, 5 cards each (total 50). num_decks should auto-adjust to 2.
    # Logic: total_drawn > 48.
    room = manager.create_room(username="Player1", max_players=10, num_decks=1, initial_hand_size=5)
    # Add 9 more players
    for i in range(2, 11):
        manager.join_room(room.room_id, f"Player{i}")

    manager.start_game(room.room_id)
    assert room.num_decks == 2
    assert len(room.game_state.deck) == (2 * 54) - (10 * 5) - 1

def test_deck_no_adjustment_if_already_two():
    manager = GameRoomManager()

    # Test Case 4: 2 players, 4 cards each, but num_decks manually set to 2. num_decks should remain 2.
    room = manager.create_room(username="Player1", max_players=4, num_decks=2, initial_hand_size=4)
    manager.join_room(room.room_id, "Player2")

    manager.start_game(room.room_id)
    assert room.num_decks == 2
    assert len(room.game_state.deck) == (2 * 54) - (2 * 4) - 1
