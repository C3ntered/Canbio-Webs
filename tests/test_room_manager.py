import pytest
from fastapi import HTTPException
from backend import GameRoomManager, GameStatus

def test_join_room_full():
    manager = GameRoomManager()
    # Create a room with max_players=1. The creator is already in the room.
    room = manager.create_room(username="player1", max_players=1)
    room_id = room.room_id

    # Attempt to join the room with a second user
    with pytest.raises(HTTPException) as excinfo:
        manager.join_room(room_id, username="player2")

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Room is full"

def test_join_room_already_started():
    manager = GameRoomManager()
    # Create a room. creator is player1.
    room = manager.create_room(username="player1", max_players=2)
    room_id = room.room_id
    # Join with player2 to meet min_players=2 (default)
    manager.join_room(room_id, username="player2")

    # Start the game
    manager.start_game(room_id)
    assert manager.rooms[room_id].status == GameStatus.PLAYING

    # Attempt to join the room after it has started
    with pytest.raises(HTTPException) as excinfo:
        manager.join_room(room_id, username="player3")

    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "Game already started"

def test_join_room_not_found():
    manager = GameRoomManager()
    # Attempt to join a non-existent room ID
    with pytest.raises(HTTPException) as excinfo:
        manager.join_room("non-existent-id", username="player1")

    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Room not found"
