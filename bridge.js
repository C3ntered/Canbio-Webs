// Auto-detect API base URL from current page location
// Works when HTML is served via HTTP (recommended) or falls back to localhost for file://
const getApiBase = () => {
    // If accessed via file:// protocol, default to localhost
    if (window.location.protocol === 'file:') {
        return 'http://localhost:8000';
    }
    // Otherwise use the same protocol and hostname (assumes API is on same server/port)
    // For development, if HTML is served from port 8000, API is also on 8000
    const protocol = window.location.protocol;
    const hostname = window.location.hostname;
    const port = window.location.port || (protocol === 'https:' ? '443' : '80');
    // If port is different (e.g., HTML on 3000, API on 8000), use port 8000
    return `${protocol}//${hostname}:8000`;
};

const API_BASE = getApiBase();

let socket = null;
let playerContext = {
    username: null,
    roomId: null,
    playerId: null,
};
let latestRoomState = null;
let pendingDrawnCard = null;  // Card drawn, awaiting swap or discard
let pendingAbility = null;    // Ability available to use
let selectingTargets = false; // Mode for selecting targets
let selectedTargets = [];     // Targets selected so far

async function joinGame(username, roomId = null) {
    if (!username) {
        throw new Error('Username is required');
    }

    const endpoint = roomId ? `/api/rooms/${roomId}/join` : '/api/rooms';
    const payload = roomId ? { username } : { username, max_players: 4 };

    const response = await fetch(`${API_BASE}${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });

    if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || 'Unable to join room');
    }

    const data = await response.json();
    
    // Handle different response formats: create room returns Room directly, join returns {room, player_id}
    let room, playerId, joinedRoomId;
    
    if (roomId) {
        // Joining an existing room
        room = data.room;
        playerId = data.player_id;
        joinedRoomId = roomId;
    } else {
        // Creating a new room - Room object is returned directly
        room = data;
        joinedRoomId = room.room_id;
        // Find the player by username to get their player_id
        const player = room.players.find(p => p.username === username);
        if (!player) {
            throw new Error('Could not find player in created room');
        }
        playerId = player.player_id;
    }

    playerContext = {
        username,
        roomId: joinedRoomId,
        playerId: playerId
    };

    setupWebSocket(joinedRoomId, playerId);
    renderBoard(room, playerId);
}

function setupWebSocket(roomId, playerId) {
    if (socket) {
        socket.close();
    }

    // Use WebSocket protocol matching the API_BASE protocol
    const wsProtocol = API_BASE.startsWith('https') ? 'wss:' : 'ws:';
    const wsHost = API_BASE.replace(/^https?:\/\//, '').replace(/^wss?:\/\//, '');
    socket = new WebSocket(`${wsProtocol}//${wsHost}/ws/${roomId}`);

    socket.addEventListener('open', () => {
        const joinMsg = {
            type: 'join',
            data: { player_id: playerId }
        };
        socket.send(JSON.stringify(joinMsg));
    });

    socket.addEventListener('message', handleSocketMessage);
    socket.addEventListener('close', () => updateStatus('Disconnected'));
    socket.addEventListener('error', () => updateStatus('Connection error'));
}

function handleSocketMessage(event) {
    const message = JSON.parse(event.data);
    console.log('New Update:', message);

    switch (message.type) {
        case 'game_state':
        case 'card_played':
        case 'card_eliminated':
        case 'player_drew_card':
        case 'player_penalty_draw':
            if (message.data.player_id !== playerContext.playerId) {
                notify(message.data.message || 'A player played the wrong card and drew a penalty!');
            }
        case 'deck_reshuffled':
        case 'cambio_called':
        case 'game_ended':
        case 'game_started':
        case 'round_started':
        case 'turn_ended':
            pendingDrawnCard = null;
            pendingAbility = null;
            selectingTargets = false;
            selectedTargets = [];
            latestRoomState = message.data.room;
            renderBoard(message.data.room, message.data.your_player_id || playerContext.playerId);
            break;
        case 'wrong_sacrifice_penalty':
            pendingDrawnCard = null;
            alert(message.data.message || 'Wrong card! You drew a penalty card.');
            latestRoomState = message.data.room;
            renderBoard(message.data.room, playerContext.playerId);
            notify(`Penalty: You drew ${formatCard(message.data.card)}`);
            break;
        case 'card_drawn':
            pendingDrawnCard = message.data.card;
            latestRoomState = message.data.room;
            notify(`You drew ${formatCard(message.data.card)}. Choose: swap with a hand card or discard.`);
            renderBoard(message.data.room, playerContext.playerId);
            break;
        case 'ability_opportunity':
            pendingDrawnCard = null; // Clear drawn card state to hide draw panel
            pendingAbility = message.data.ability;
            notify(message.data.message);
            latestRoomState = message.data.room;
            renderBoard(message.data.room, playerContext.playerId);
            startAbilitySelection(pendingAbility);
            break;
        case 'ability_resolution':
            const { ability, card, card_index, target_player_id, duration, first, second } = message.data;

            if (ability === 'peek_self' && playerContext.playerId === target_player_id) {
                const cardContainer = document.getElementById('card-container');
                if (cardContainer && cardContainer.children[card_index]) {
                    const cardButton = cardContainer.children[card_index];
                    const originalText = cardButton.innerText; // "Card Back"
                    
                    cardButton.innerText = formatCard(card);
                    cardButton.classList.add('revealed');

                    setTimeout(() => {
                        cardButton.innerText = originalText;
                        cardButton.classList.remove('revealed');
                    }, duration || 5000);
                }
            } else if (ability === 'peek_other') {
                if (latestRoomState) {
                    const targetPlayer = latestRoomState.players.find(p => p.player_id === target_player_id);
                    const targetUsername = targetPlayer ? targetPlayer.username : 'another player';
                    const text = `You see ${targetUsername}'s card #${card_index + 1}: ${formatCard(card)}`;
                    notify(text, duration || 5000);
                    alert(text); // Also show alert for explicit visibility
                }
            } else if (ability === 'look_and_swap' && first && second) {
                const p1 = latestRoomState.players.find(p => p.player_id === first.player_id);
                const p2 = latestRoomState.players.find(p => p.player_id === second.player_id);
                const msg = `Card 1 (${p1.username}): ${formatCard(first.card)}\nCard 2 (${p2.username}): ${formatCard(second.card)}`;
                alert("Memorize these cards:\n" + msg);
                notify(msg.replace('\n', ', '), 10000);
            } else {
                notify(`Ability result: ${JSON.stringify(message.data)}`);
            }
            break;
        case 'player_joined':
            notify(`${message.data.username} joined the room`);
            if (message.data.room) {
                latestRoomState = message.data.room;
                renderBoard(message.data.room, playerContext.playerId);
            } else if (latestRoomState) {
                renderBoard(latestRoomState, playerContext.playerId);
            }
            break;
        case 'player_left':
            notify(`Player ${message.data.player_id} left the room`);
            if (message.data.room) {
                latestRoomState = message.data.room;
                renderBoard(message.data.room, playerContext.playerId);
            } else if (latestRoomState) {
                renderBoard(latestRoomState, playerContext.playerId);
            }
            break;
        case 'error':
            alert(message.message);
            break;
        default:
            console.warn('Unhandled message', message);
    }
}

function sendMessage(type, data = {}) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
        throw new Error('WebSocket is not connected');
    }
    socket.send(JSON.stringify({ type, data }));
}

function drawCard() {
    sendMessage('draw_card');
}

function resolveDraw(action, cardIndex) {
    const payload = { action };
    if (action === 'swap' && cardIndex !== undefined) {
        payload.card_index = cardIndex;
    }
    sendMessage('resolve_draw', payload);
}

function playCard(card, abilityPayload = null) {
    const payload = { card };
    if (abilityPayload) {
        payload.ability = abilityPayload;
    }
    sendMessage('play_card', payload);
}

function eliminateCard(targetPlayerId, cardIndex) {
    sendMessage('eliminate_card', {
        target_player_id: targetPlayerId,
        card_index: cardIndex
    });
}

function callCambio() {
    sendMessage('call_cambio');
}

function startGame() {
    sendMessage('start_game');
}

function skipAbility() {
    sendMessage('skip_ability');
    pendingAbility = null;
    selectingTargets = false;
    selectedTargets = [];
    const panel = document.getElementById('ability-panel');
    if (panel) panel.style.display = 'none';
}

function startAbilitySelection(ability) {
    selectingTargets = true;
    selectedTargets = [];
    const panel = document.getElementById('ability-panel');
    const nameDisplay = document.getElementById('ability-name-display');
    const desc = document.getElementById('ability-desc');
    const controls = document.getElementById('ability-controls');

    if (panel && nameDisplay && desc) {
        panel.style.display = 'block';
        nameDisplay.innerText = ability;
        if (controls) controls.innerHTML = '';

        let instructions = "";
        if (ability === 'peek_self') instructions = "Click one of YOUR cards to peek at it.";
        else if (ability === 'peek_other') instructions = "Click one of an OPPONENT'S cards to peek at it.";
        else if (ability === 'blind_swap') instructions = "Click one of YOUR cards, then one of an OPPONENT'S cards to swap them.";
        else if (ability === 'look_and_swap') instructions = "Click ANY two cards to look at them. (Swap optional)";

        desc.innerText = instructions;
    }
}

function handleCardClick(playerId, cardIndex, isOwnCard) {
    if (!selectingTargets) return;

    // Add target
    selectedTargets.push({ player_id: playerId, card_index: cardIndex });

    // Check if we have enough targets
    if (pendingAbility === 'peek_self') {
        if (!isOwnCard) {
            alert("Please select one of YOUR cards.");
            selectedTargets = [];
            return;
        }
        sendMessage('use_ability', { card_index: cardIndex });
        selectingTargets = false;
        pendingAbility = null; // Wait for result
    } else if (pendingAbility === 'peek_other') {
        if (isOwnCard) {
            alert("Please select an OPPONENT'S card.");
            selectedTargets = [];
            return;
        }
        sendMessage('use_ability', { target_player_id: playerId, card_index: cardIndex });
        selectingTargets = false;
        pendingAbility = null;
    } else if (pendingAbility === 'blind_swap') {
        if (selectedTargets.length === 1) {
             if (!isOwnCard) {
                 alert("First select one of YOUR cards.");
                 selectedTargets = [];
             } else {
                 notify("Select an opponent's card to swap with.");
             }
        } else if (selectedTargets.length === 2) {
            const first = selectedTargets[0];
            const second = selectedTargets[1];
            if (first.player_id === second.player_id) {
                alert("You must swap with an opponent.");
                selectedTargets = [first]; // Keep first
                return;
            }
            sendMessage('use_ability', {
                target_player_id: second.player_id,
                own_card_index: first.card_index,
                target_card_index: second.card_index
            });
            selectingTargets = false;
            pendingAbility = null;
        }
    } else if (pendingAbility === 'look_and_swap') {
        if (selectedTargets.length < 2) {
            notify(`Selected ${selectedTargets.length}/2 cards.`);
        } else {
            // Ask for swap immediately? Or just look?
            // "Look and swap (look at any two cards and decide whether you want to swap them.)"
            // For now, prompt user:
            const doSwap = confirm("Do you want to swap these two cards after looking?");
            sendMessage('use_ability', {
                first_target: selectedTargets[0],
                second_target: selectedTargets[1],
                swap: doSwap
            });
            selectingTargets = false;
            pendingAbility = null;
        }
    }
}

function renderBoard(room, yourPlayerId) {
    if (!room) {
        return;
    }
    latestRoomState = room;
    const lobby = document.getElementById('lobby');
    const board = document.getElementById('game-board');
    if (lobby && board) {
        lobby.style.display = 'none';
        board.style.display = 'block';
    }

    // Update room ID display
    const roomIdDisplay = document.getElementById('room-id-display');
    if (roomIdDisplay && room.room_id) {
        roomIdDisplay.innerText = room.room_id;
    }

    // Update player list
    const playerListContainer = document.getElementById('player-list');
    if (playerListContainer) {
        playerListContainer.innerHTML = '';
        const listTitle = document.createElement('h3');
        listTitle.innerText = `Players (${room.players.length}/${room.max_players})`;
        playerListContainer.appendChild(listTitle);
        const list = document.createElement('ul');
        list.style.listStyle = 'none';
        list.style.padding = '0';
        room.players.forEach(player => {
            const item = document.createElement('li');
            item.style.padding = '8px';
            item.style.margin = '5px 0';
            item.style.backgroundColor = player.player_id === yourPlayerId ? '#e3f2fd' : '#f5f5f5';
            item.style.borderRadius = '4px';
            item.innerText = player.username + (player.player_id === yourPlayerId ? ' (You)' : '');
            if (player.is_connected) {
                item.innerHTML += ' <span style="color: green;">●</span>';
            }
            list.appendChild(item);
        });
        playerListContainer.appendChild(list);
    }

    // Show/hide Start Game button based on game status
    const startGameBtn = document.getElementById('start-game-btn');
    if (startGameBtn) {
        const isWaiting = room.status === 'waiting' || room.status === 'WAITING';
        if (isWaiting) {
            if (room.players.length >= room.min_players) {
                startGameBtn.style.display = 'block';
                startGameBtn.disabled = false;
                startGameBtn.title = 'Click to start the game';
            } else {
                startGameBtn.style.display = 'block';
                startGameBtn.disabled = true;
                startGameBtn.title = `Need at least ${room.min_players} players to start (currently ${room.players.length})`;
            }
        } else {
            startGameBtn.style.display = 'none';
        }
    }

    const turnIndicator = document.getElementById('turn-indicator');
    if (turnIndicator) {
        const isWaiting = room.status === 'waiting' || room.status === 'WAITING';
        const isViewingPhase = room.game_state?.viewing_phase;
        if (isWaiting) {
            turnIndicator.innerText = `Waiting for players to join... (${room.players.length}/${room.max_players})`;
        } else if (isViewingPhase) {
            turnIndicator.innerText = 'Memorize your bottom 2 cards! (5 seconds)';
            turnIndicator.style.backgroundColor = '#FF9800';
        } else {
            turnIndicator.style.backgroundColor = '#2196F3';
            const currentPlayer = room.players.find((p) => p.player_id === room.game_state.current_turn);
            if (currentPlayer) {
                if (currentPlayer.player_id === yourPlayerId) {
                    turnIndicator.innerText = 'Your turn';
                } else {
                    turnIndicator.innerText = `Waiting for ${currentPlayer.username}`;
                }
            } else {
                turnIndicator.innerText = 'Waiting for players...';
            }
        }
    }

    // Only show game elements when game is playing (or in viewing phase for cards)
    const discardPileContainer = document.getElementById('discard-pile');
    const myHandContainer = document.getElementById('my-hand');
    const opponentsHandsContainer = document.getElementById('opponents-hands');
    const actionButtons = document.querySelector('.action-buttons');
    const isPlaying = room.status === 'playing' || room.status === 'PLAYING';
    const isViewingPhase = room.game_state?.viewing_phase;
    
    // Hide countdown when not in viewing phase
    const countdownEl = document.getElementById('viewing-countdown');
    if (countdownEl && !isViewingPhase) countdownEl.style.display = 'none';
    
    if (isPlaying) {
        if (discardPileContainer) discardPileContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (myHandContainer) myHandContainer.style.display = 'block';
        if (opponentsHandsContainer) opponentsHandsContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (actionButtons) actionButtons.style.display = isViewingPhase ? 'none' : 'flex';

        // Draw choice panel - show when we have a pending drawn card
        const drawChoicePanel = document.getElementById('draw-choice-panel');
        const drawnCardDisplay = document.getElementById('drawn-card-display');
        const discardDrawnBtn = document.getElementById('discard-drawn-btn');
        const drawCardBtn = document.getElementById('draw-card-btn');
        if (drawChoicePanel && drawnCardDisplay) {
            if (pendingDrawnCard) {
                drawChoicePanel.style.display = 'block';
                drawnCardDisplay.textContent = formatCard(pendingDrawnCard);
                if (drawCardBtn) drawCardBtn.disabled = true;
            } else {
                drawChoicePanel.style.display = 'none';
                if (drawCardBtn) drawCardBtn.disabled = false;
            }
        }
        
        // Ability panel visibility
        const abilityPanel = document.getElementById('ability-panel');
        if (abilityPanel) {
            abilityPanel.style.display = pendingAbility ? 'block' : 'none';
        }

        const topCardContainer = document.getElementById('top-card');
        if (topCardContainer) {
            const topCard = room.game_state.discard_pile.slice(-1)[0];
            topCardContainer.innerText = topCard ? formatCard(topCard) : 'N/A';
        }

        const cardContainer = document.getElementById('card-container');
        if (cardContainer) {
            cardContainer.innerHTML = '';
            cardContainer.className = ''; // reset
            const me = room.players.find((p) => p.player_id === yourPlayerId);
            
            // Check for viewing phase - show bottom 2 cards for 5 seconds
            if (isViewingPhase && !document.getElementById('viewing-timer-active')) {
                // Mark timer as active to prevent duplicates
                const marker = document.createElement('div');
                marker.id = 'viewing-timer-active';
                document.body.appendChild(marker);
                
                // Countdown display
                const countdownEl = document.getElementById('viewing-countdown');
                if (countdownEl) {
                    let secs = 5;
                    countdownEl.textContent = secs;
                    countdownEl.style.display = 'block';
                    const countInterval = setInterval(() => {
                        secs--;
                        countdownEl.textContent = secs;
                        if (secs <= 0) clearInterval(countInterval);
                    }, 1000);
                }
                
                setTimeout(() => {
                    try { sendMessage('end_viewing'); } catch (e) { /* ws may be closed */ }
                    if (marker.parentNode) marker.parentNode.removeChild(marker);
                    const ce = document.getElementById('viewing-countdown');
                    if (ce) ce.style.display = 'none';
                }, 5000);
            }

            if (me) {
                const isAwaitingDrawChoice = !!pendingDrawnCard;
                cardContainer.classList.add(me.hand.length >= 5 ? 'cols-3' : 'cols-2');
                me.hand.forEach((card, index) => {
                    const btn = document.createElement('button');
                    // 2x2 matrix: indices 0,1 = top row; 2,3 = bottom row. Bottom two shown for 5 seconds.
                    const isBottomCard = index === 2 || index === 3;
                    const isVisible = isViewingPhase && isBottomCard;
                    btn.innerText = isVisible ? formatCard(card) : "🂠";
                    btn.title = isViewingPhase ? (isBottomCard ? 'Memorize this card!' : 'Face down') : (isAwaitingDrawChoice ? `Click to swap with drawn card` : `Card #${index + 1}`);
                    if (!isViewingPhase) {
                        if (selectingTargets) {
                            btn.addEventListener('click', () => handleCardClick(yourPlayerId, index, true));
                            btn.style.borderColor = "#00acc1";
                            btn.style.cursor = "pointer";
                            btn.innerText = "🎯";
                        } else if (isAwaitingDrawChoice) {
                            btn.addEventListener('click', () => resolveDraw('swap', index));
                        } else {
                            btn.addEventListener('click', () => playCard(card));
                        }
                    } else {
                        btn.style.cursor = 'default';
                        btn.disabled = true;
                    }
                    cardContainer.appendChild(btn);
                });
            }
        }

        // Opponents' hands - face down, clickable to eliminate when it's your turn
        const opponentsContainer = document.getElementById('opponents-container');
        if (opponentsContainer && !isViewingPhase) {
            opponentsContainer.innerHTML = '';
            const mustResolveDraw = !!pendingDrawnCard;
            room.players.forEach(player => {
                if (player.player_id === yourPlayerId) return; // Skip self

                const section = document.createElement('div');
                section.className = 'opponent-hand';
                const nameEl = document.createElement('div');
                nameEl.className = 'opponent-name';
                nameEl.innerText = player.username + (player.is_connected ? ' ●' : '');
                section.appendChild(nameEl);
                const cardsDiv = document.createElement('div');
                cardsDiv.className = 'opponent-cards';
                player.hand.forEach((_, index) => {
                    const btn = document.createElement('button');
                    btn.innerText = '🂠';
                    btn.title = !mustResolveDraw ? `Try to eliminate ${player.username}'s card #${index + 1} (must match discard)` : (mustResolveDraw ? 'Resolve your drawn card first' : 'Face down');

                    if (selectingTargets) {
                         btn.addEventListener('click', () => handleCardClick(player.player_id, index, false));
                         btn.style.borderColor = "#00acc1";
                         btn.style.cursor = "pointer";
                         btn.innerText = "🎯";
                    } else if (!mustResolveDraw) {
                        btn.addEventListener('click', () => eliminateCard(player.player_id, index));
                    } else {
                        btn.disabled = true;
                    }
                    cardsDiv.appendChild(btn);
                });
                section.appendChild(cardsDiv);
                opponentsContainer.appendChild(section);
            });
        } else if (opponentsContainer && isViewingPhase) {
            opponentsContainer.innerHTML = '';
        }
    } else {
        if (discardPileContainer) discardPileContainer.style.display = 'none';
        if (myHandContainer) myHandContainer.style.display = 'none';
        if (opponentsHandsContainer) opponentsHandsContainer.style.display = 'none';
    }
}

function formatCard(card) {
    return `${card.rank} of ${card.suit}`;
}

function notify(text, duration = null) {
    console.log(text);
    const area = document.getElementById('notifications');
    if (area) {
        const item = document.createElement('div');
        item.innerText = text;
        area.prepend(item);

        if (duration) {
            setTimeout(() => {
                if (item.parentNode) {
                    item.parentNode.removeChild(item);
                }
            }, duration);
        }
    }
}

function updateStatus(status) {
    const statusEl = document.getElementById('status');
    if (statusEl) {
        statusEl.innerText = status;
    }
}

function copyRoomId() {
    const roomId = playerContext.roomId;
    if (!roomId) {
        alert('No room ID available');
        return;
    }

    // Copy to clipboard
    navigator.clipboard.writeText(roomId).then(() => {
        const button = document.getElementById('copy-room-id');
        if (button) {
            const originalText = button.innerText;
            button.innerText = 'Copied!';
            button.classList.add('copied');
            setTimeout(() => {
                button.innerText = originalText;
                button.classList.remove('copied');
            }, 2000);
        }
        notify(`Room ID "${roomId}" copied to clipboard!`);
    }).catch(err => {
        // Fallback for older browsers
        const textarea = document.createElement('textarea');
        textarea.value = roomId;
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand('copy');
            notify(`Room ID "${roomId}" copied to clipboard!`);
        } catch (e) {
            alert(`Room ID: ${roomId}\n\n(Please copy this manually)`);
        }
        document.body.removeChild(textarea);
    });
}

async function handleJoin() {
    const usernameInput = document.getElementById('username');
    const roomInput = document.getElementById('room-id');
    const username = usernameInput ? usernameInput.value.trim() : '';
    const roomId = roomInput ? roomInput.value.trim() : null;
    try {
        await joinGame(username, roomId || null);
        updateStatus(`Joined room ${playerContext.roomId}`);
    } catch (error) {
        alert(error.message);
    }
}

window.joinGame = joinGame;
window.handleJoin = handleJoin;
window.drawCard = drawCard;
window.playCard = playCard;
window.eliminateCard = eliminateCard;
window.resolveDraw = resolveDraw;
window.callCambio = callCambio;
window.copyRoomId = copyRoomId;
window.startGame = startGame;
window.skipAbility = skipAbility;
