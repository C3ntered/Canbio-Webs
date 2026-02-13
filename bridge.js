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
let pendingSwapDecision = false; // Mode for deciding whether to swap
let eliminationTarget = null; // Target for elimination (waiting for replacement card selection)
let adminMode = false; // Admin debug mode to see all cards

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
            // Show winner only if we have a winner
            if (message.data.winner_id) {
                const winnerId = message.data.winner_id;
                const winnerName = message.data.winner_username;
                alert(`Game Over! Winner: ${winnerName}`);
                notify(`Game Over! Winner: ${winnerName} (Score: ${latestRoomState.players.find(p=>p.player_id === winnerId)?.score})`, 10000);
            }

            pendingDrawnCard = null;
            pendingAbility = null;
            selectingTargets = false;
            selectedTargets = [];
            pendingSwapDecision = false;
            eliminationTarget = null;
            latestRoomState = message.data.room;
            renderBoard(message.data.room, message.data.your_player_id || playerContext.playerId);
            break;
        case 'game_reset':
            notify(message.data.message);
            latestRoomState = message.data.room;
            renderBoard(message.data.room, playerContext.playerId);

            // Highlight swapped cards
            const { player1_id, card1_index, player2_id, card2_index } = message.data;
            if (player1_id !== undefined && card1_index !== undefined &&
                player2_id !== undefined && card2_index !== undefined) {

                const highlight = (pid, idx) => {
                     // Need to find the button element in the DOM.
                     // The renderBoard function rebuilds the DOM, so we must find elements in the *new* DOM.
                     // We added 'data-index' attribute to buttons in renderBoard to help with this.
                     let btn = null;
                     if (pid === playerContext.playerId) {
                         const container = document.getElementById('card-container');
                         if (container) {
                             const buttons = Array.from(container.children);
                             btn = buttons.find(b => parseInt(b.getAttribute('data-index')) === idx);
                         }
                     } else {
                        const oppContainer = document.getElementById('opponents-container');
                        if (oppContainer) {
                            // Find the opponent's container
                            // Note: renderBoard iterates players to build this.
                            // We need to find the correct .opponent-hand div.
                            const allPlayers = latestRoomState.players;
                            const opponents = allPlayers.filter(p => p.player_id !== playerContext.playerId);
                            const oppIndex = opponents.findIndex(p => p.player_id === pid);

                            if (oppIndex !== -1 && oppContainer.children[oppIndex]) {
                                const cardsDiv = oppContainer.children[oppIndex].querySelector('.opponent-cards');
                                if (cardsDiv) {
                                     const buttons = Array.from(cardsDiv.children);
                                     btn = buttons.find(b => parseInt(b.getAttribute('data-index')) === idx);
                                }
                            }
                        }
                     }

                     if (btn) {
                         btn.classList.add('swapped-highlight');
                         setTimeout(() => btn.classList.remove('swapped-highlight'), 3000);
                     }
                };

                highlight(player1_id, card1_index);
                highlight(player2_id, card2_index);
            }
            break;
        case 'game_started':
        case 'round_started':
        case 'turn_ended':
            pendingDrawnCard = null;
            pendingAbility = null;
            selectingTargets = false;
            selectedTargets = [];
            pendingSwapDecision = false;
            eliminationTarget = null;
            latestRoomState = message.data.room;
            renderBoard(message.data.room, message.data.your_player_id || playerContext.playerId);
            break;
        case 'wrong_sacrifice_penalty':
            pendingDrawnCard = null;
            alert(message.data.message || 'Wrong card! You drew a penalty card.');
            latestRoomState = message.data.room;
            renderBoard(message.data.room, playerContext.playerId);
            notify(`Penalty: You drew a face-down penalty card.`);
            break;
        case 'cards_swapped':
            notify(message.data.message);
            latestRoomState = message.data.room;
            renderBoard(message.data.room, playerContext.playerId);

            // Highlight swapped cards
            const { player1_id: player1Id, card1_index: card1Index, player2_id: player2Id, card2_index: card2Index } = message.data;

            const highlight = (pid, idx) => {
                 let btn = null;
                 if (pid === playerContext.playerId) {
                     const container = document.getElementById('card-container');
                     if (container) {
                         const buttons = Array.from(container.children);
                         btn = buttons.find(b => parseInt(b.getAttribute('data-index')) === idx);
                     }
                 } else {
                    const oppContainer = document.getElementById('opponents-container');
                    if (oppContainer) {
                        const allPlayers = latestRoomState.players;
                        const opponents = allPlayers.filter(p => p.player_id !== playerContext.playerId);
                        const oppIndex = opponents.findIndex(p => p.player_id === pid);

                        if (oppIndex !== -1 && oppContainer.children[oppIndex]) {
                            const cardsDiv = oppContainer.children[oppIndex].querySelector('.opponent-cards');
                            if (cardsDiv) {
                                 const buttons = Array.from(cardsDiv.children);
                                 btn = buttons.find(b => parseInt(b.getAttribute('data-index')) === idx);
                            }
                        }
                    }
                 }

                 if (btn) {
                     btn.classList.add('swapped-highlight');
                     setTimeout(() => btn.classList.remove('swapped-highlight'), 3000);
                 }
            };

            if (player1_id !== undefined && card1_index !== undefined) highlight(player1_id, card1_index);
            if (player2_id !== undefined && card2_index !== undefined) highlight(player2_id, card2_index);
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
            startAbilitySelection(pendingAbility);
            renderBoard(message.data.room, playerContext.playerId);
            break;
        case 'ability_resolution':
            const { ability, card, card_index, target_player_id, duration, first, second } = message.data;

            if (ability === 'peek_self' && playerContext.playerId === target_player_id) {
                const text = `You peeked at your card #${card_index + 1}: ${formatCard(card)}`;
                notify(text, duration || 5000);

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
                // Reveal the cards visually
                const revealCard = (pid, idx, c) => {
                    const selector = pid === playerContext.playerId
                        ? `#card-container > button:nth-child(${idx + 1})`
                        : `#opponents-container .opponent-hand:has(.opponent-name:contains('${latestRoomState.players.find(p => p.player_id === pid)?.username}')) .opponent-cards button:nth-child(${idx + 1})`;

                    // Simple logic for finding the button. For opponents, it's tricky with :contains, let's just use index if we can find the player section.
                    let btn = null;
                    if (pid === playerContext.playerId) {
                         const container = document.getElementById('card-container');
                         if (container) btn = container.children[idx];
                    } else {
                        // Find opponent section
                        const oppContainer = document.getElementById('opponents-container');
                        if (oppContainer) {
                            const oppIndex = latestRoomState.players.filter(p => p.player_id !== playerContext.playerId).findIndex(p => p.player_id === pid);
                            if (oppIndex !== -1 && oppContainer.children[oppIndex]) {
                                const cardsDiv = oppContainer.children[oppIndex].querySelector('.opponent-cards');
                                if (cardsDiv) btn = cardsDiv.children[idx];
                            }
                        }
                    }

                    if (btn) {
                        const originalText = btn.innerText;
                        btn.innerText = formatCard(c);
                        btn.classList.add('revealed');
                        setTimeout(() => {
                            btn.innerText = originalText;
                            btn.classList.remove('revealed');
                        }, 5000);
                    }
                };

                revealCard(first.player_id, first.card_index, first.card);
                revealCard(second.player_id, second.card_index, second.card);

                // Show swap decision UI
                pendingSwapDecision = true;
                const p1 = latestRoomState.players.find(p => p.player_id === first.player_id);
                const p2 = latestRoomState.players.find(p => p.player_id === second.player_id);
                notify(`You saw: ${formatCard(first.card)} (${p1.username}) and ${formatCard(second.card)} (${p2.username})`);

                renderBoard(latestRoomState, playerContext.playerId);
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

function drawFromDiscard() {
    sendMessage('draw_from_discard');
}

function resolveDraw(action, cardIndex) {
    const payload = { action };
    if (action === 'swap' && cardIndex !== undefined) {
        payload.card_index = cardIndex;
    }
    sendMessage('resolve_draw', payload);
}

function playCard(card, cardIndex, abilityPayload = null) {
    const payload = { card };
    if (cardIndex !== undefined && cardIndex !== null) {
        payload.card_index = cardIndex;
    }
    if (abilityPayload) {
        payload.ability = abilityPayload;
    }
    sendMessage('play_card', payload);
}

function startElimination(targetPlayerId, cardIndex) {
    eliminationTarget = { pid: targetPlayerId, idx: cardIndex };
    notify("Target selected! Now click one of YOUR cards to give to them.");
    renderBoard(latestRoomState, playerContext.playerId);
}

function completeElimination(replacementCardIndex) {
    if (!eliminationTarget) return;
    sendMessage('eliminate_card', {
        target_player_id: eliminationTarget.pid,
        card_index: eliminationTarget.idx,
        replacement_card_index: replacementCardIndex
    });
    eliminationTarget = null;
}

function callCambio() {
    sendMessage('call_cambio');
}

function startGame() {
    sendMessage('start_game');
}

function playAgain() {
    sendMessage('play_again');
}

function resolveSwapDecision(doSwap) {
    sendMessage('resolve_swap_decision', { swap: doSwap });
    pendingSwapDecision = false;
    const panel = document.getElementById('ability-panel');
    if (panel) panel.style.display = 'none';
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
            // Just send the targets. The decision comes later.
            sendMessage('use_ability', {
                first_target: selectedTargets[0],
                second_target: selectedTargets[1]
            });
            selectingTargets = false;
            pendingAbility = null;
        }
    }
}

function getVisualOrder(totalCards) {
    // Generate rendering order for grid-auto-flow: column with 2 fixed rows.
    // We want the first 4 cards (indices 0-3) to visually appear as a 2x2 grid (0,1 Top; 2,3 Bottom).
    // With column flow, indices fill: (1,1), (2,1), (1,2), (2,2)...
    // So we need to feed: 0, 2, 1, 3.
    // Subsequent cards fill columns sequentially: 4 (Top), 5 (Bottom), etc.
    const indices = [];
    if (totalCards > 0) indices.push(0);
    if (totalCards > 2) indices.push(2);
    if (totalCards > 1) indices.push(1);
    if (totalCards > 3) indices.push(3);

    for (let i = 4; i < totalCards; i++) {
        indices.push(i);
    }
    return indices.filter(i => i < totalCards);
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
        const isFinished = room.status === 'finished' || room.status === 'FINISHED';

        if (isWaiting) {
            if (room.players.length >= room.min_players) {
                startGameBtn.style.display = 'block';
                startGameBtn.disabled = false;
                startGameBtn.title = 'Click to start the game';
                startGameBtn.innerText = 'Start Game';
                startGameBtn.onclick = startGame;
            } else {
                startGameBtn.style.display = 'block';
                startGameBtn.disabled = true;
                startGameBtn.title = `Need at least ${room.min_players} players to start (currently ${room.players.length})`;
                startGameBtn.innerText = 'Start Game';
                startGameBtn.onclick = null;
            }
        } else if (isFinished) {
            startGameBtn.style.display = 'block';
            startGameBtn.disabled = false;
            startGameBtn.innerText = 'Play Again';
            startGameBtn.onclick = playAgain;
            startGameBtn.title = 'Click to return to lobby and play again';
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

                // If drawn from discard pile, you cannot discard it again
                if (discardDrawnBtn) {
                     // Check if last draw source was discard
                     const me = room.players.find(p => p.player_id === yourPlayerId);
                     if (me && me.last_draw_source === 'discard') {
                         discardDrawnBtn.style.display = 'none';
                         // Add note
                         let note = document.getElementById('swap-only-note');
                         if (!note) {
                             note = document.createElement('p');
                             note.id = 'swap-only-note';
                             note.style.color = 'red';
                             note.innerText = 'You must swap when drawing from discard pile.';
                             drawChoicePanel.appendChild(note);
                         } else {
                             note.style.display = 'block';
                         }
                     } else {
                         discardDrawnBtn.style.display = 'inline-block';
                         const note = document.getElementById('swap-only-note');
                         if (note) note.style.display = 'none';
                     }
                }

                if (drawCardBtn) drawCardBtn.disabled = true;
                const drawDiscardBtn = document.getElementById('draw-discard-btn');
                if (drawDiscardBtn) drawDiscardBtn.disabled = true;
            } else {
                drawChoicePanel.style.display = 'none';
                if (drawCardBtn) drawCardBtn.disabled = false;
                const drawDiscardBtn = document.getElementById('draw-discard-btn');
                if (drawDiscardBtn) drawDiscardBtn.disabled = false;
            }
        }
        
        // Ability panel visibility
        const abilityPanel = document.getElementById('ability-panel');
        if (abilityPanel) {
            if (pendingSwapDecision) {
                abilityPanel.style.display = 'block';
                const nameDisplay = document.getElementById('ability-name-display');
                const desc = document.getElementById('ability-desc');
                const controls = document.getElementById('ability-controls');

                if (nameDisplay) nameDisplay.innerText = "Swap Decision";
                if (desc) desc.innerText = "Do you want to swap the cards you just saw?";
                if (controls) {
                    controls.innerHTML = `
                        <button onclick="resolveSwapDecision(true)" style="background-color: #4CAF50; margin-right: 10px;">Swap</button>
                        <button onclick="resolveSwapDecision(false)" style="background-color: #f44336;">Keep</button>
                    `;
                }
                // Hide Skip Ability button during decision if possible, or repurpose it?
                // The main skip button is outside controls div in HTML. We might want to hide it.
                // But let's leave it for now.
            } else {
                abilityPanel.style.display = pendingAbility ? 'block' : 'none';
                // Clear controls if not decision
                const controls = document.getElementById('ability-controls');
                if (controls && !pendingAbility) controls.innerHTML = '';
            }
        }

        const callCambioBtn = document.getElementById('call-cambio-btn');
        if (callCambioBtn) {
            // Can only call if:
            // 1. It is my turn
            // 2. I haven't drawn a card yet (pendingDrawnCard is null)
            // 3. I don't have a pending ability
            // 4. Cambio hasn't been called yet
            const isMyTurn = room.game_state.current_turn === yourPlayerId;
            const canCall = isMyTurn && !pendingDrawnCard && !pendingAbility && !room.game_state.cambio_called;

            callCambioBtn.disabled = !canCall;
            if (room.game_state.cambio_called) {
                callCambioBtn.title = "Cambio has already been called";
            } else if (!isMyTurn) {
                callCambioBtn.title = "Wait for your turn";
            } else if (pendingDrawnCard || pendingAbility) {
                callCambioBtn.title = "Cannot call Cambio after drawing or during ability";
            } else {
                callCambioBtn.title = "Call Cambio to end your turn and start final round";
            }
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

                // Use visual order for rendering
                const visualOrder = getVisualOrder(me.hand.length);

                visualOrder.forEach(index => {
                    const card = me.hand[index];
                    const btn = document.createElement('button');
                    btn.setAttribute('data-index', index);

                    if (!card) {
                        // Empty slot (hole)
                        btn.innerText = "";
                        btn.disabled = true;
                        btn.style.border = "1px dashed #ccc";
                        btn.style.background = "transparent";
                        btn.style.cursor = "default";
                    } else {
                        // 2x2 matrix: indices 0,1 = top row; 2,3 = bottom row. Bottom two shown for 5 seconds.
                        const isBottomCard = index === 2 || index === 3;
                        const isVisible = (isViewingPhase && isBottomCard) || adminMode;
                        btn.innerText = isVisible ? formatCard(card) : "🂠";
                        btn.title = isViewingPhase ? (isBottomCard ? 'Memorize this card!' : 'Face down') : (isAwaitingDrawChoice ? `Click to swap with drawn card` : `Card #${index + 1}`);
                        if (adminMode) {
                            btn.style.background = "#e3f2fd";
                            btn.style.color = "#000";
                        }
                    }

                    if (card && !isViewingPhase) {
                        // Priority 1: Selecting targets (Abilities)
                        if (selectingTargets) {
                            btn.addEventListener('click', (e) => {
                                e.stopPropagation(); // Stop bubbling
                                handleCardClick(yourPlayerId, index, true);
                            });
                            btn.style.borderColor = "#00acc1";
                            btn.style.cursor = "pointer";
                            btn.innerText = "🎯";
                        }
                        // Priority 2: Swapping drawn card (Draw phase)
                        else if (isAwaitingDrawChoice) {
                            btn.addEventListener('click', () => resolveDraw('swap', index));
                        }
                        // Priority 3: Selecting replacement card for elimination
                        else if (eliminationTarget) {
                            btn.addEventListener('click', () => completeElimination(index));
                            btn.style.borderColor = "#ff9800"; // Orange highlight
                            btn.style.cursor = "pointer";
                            btn.innerText = "Give";
                            btn.title = "Give this card to replace the eliminated one";
                        }
                        // Priority 4: Default play/eliminate (Normal phase)
                        else {
                            btn.addEventListener('click', () => playCard(card, index));
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

                // Use visual order
                const visualOrder = getVisualOrder(player.hand.length);

                visualOrder.forEach(index => {
                    const card = player.hand[index];
                    const btn = document.createElement('button');
                    btn.setAttribute('data-index', index);

                    if (!card) {
                        // Empty slot
                        btn.innerText = "";
                        btn.disabled = true;
                        btn.style.border = "1px dashed #ccc";
                        btn.style.background = "transparent";
                        btn.style.cursor = "default";
                    } else {
                        if (adminMode) {
                            btn.innerText = formatCard(card);
                            btn.style.background = "#e3f2fd";
                            btn.style.color = "#000";
                            btn.style.fontSize = "14px";
                        } else {
                            btn.innerText = '🂠';
                        }
                        btn.title = !mustResolveDraw ? `Try to eliminate ${player.username}'s card #${index + 1} (must match discard)` : (mustResolveDraw ? 'Resolve your drawn card first' : 'Face down');
                    }

                    // Priority 1: Selecting targets (Abilities)
                    if (card && selectingTargets) {
                         btn.addEventListener('click', (e) => {
                             e.stopPropagation(); // Stop bubbling
                             handleCardClick(player.player_id, index, false);
                         });
                         btn.style.borderColor = "#00acc1";
                         btn.style.cursor = "pointer";
                         btn.innerText = "🎯";
                    }
                    // Priority 2: Elimination (Normal phase, if no draw pending)
                    else if (!mustResolveDraw) {
                        if (eliminationTarget && eliminationTarget.pid === player.player_id && eliminationTarget.idx === index) {
                            // Already selected as target
                            btn.style.borderColor = "#ff9800";
                            btn.style.borderWidth = "4px";
                        }
                        btn.addEventListener('click', () => startElimination(player.player_id, index));
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

function toggleAdminMode() {
    const checkbox = document.getElementById('admin-mode-toggle');
    if (checkbox) {
        adminMode = checkbox.checked;
        if (latestRoomState) {
            renderBoard(latestRoomState, playerContext.playerId);
        }
    }
}

function copyRoomId() {
    const roomId = playerContext.roomId;
    if (!roomId) {
        alert('No room ID available');
        return;
    }

    const showCopiedFeedback = () => {
        const button = document.getElementById('copy-room-id');
        if (button) {
            const originalText = button.getAttribute('data-original-text') || button.innerText;
            if (!button.getAttribute('data-original-text')) {
                button.setAttribute('data-original-text', originalText);
            }
            button.innerText = 'Copied!';
            button.classList.add('copied');
            setTimeout(() => {
                button.innerText = originalText;
                button.classList.remove('copied');
            }, 2000);
        }
        notify(`Room ID "${roomId}" copied to clipboard!`);
    };

    // Try modern Clipboard API first
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(roomId).then(showCopiedFeedback).catch(err => {
            console.warn('Clipboard API failed, trying fallback', err);
            fallbackCopy(roomId, showCopiedFeedback);
        });
    } else {
        fallbackCopy(roomId, showCopiedFeedback);
    }
}

function fallbackCopy(text, onSuccess) {
    const textarea = document.createElement('textarea');
    textarea.value = text;
    // Move offscreen but keep visible to ensure copy works
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    textarea.style.top = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();

    try {
        const successful = document.execCommand('copy');
        if (successful) {
            onSuccess();
        } else {
            prompt("Copy this Room ID:", text);
        }
    } catch (err) {
        prompt("Copy this Room ID:", text);
    }

    document.body.removeChild(textarea);
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
window.drawFromDiscard = drawFromDiscard;
window.playCard = playCard;
// window.eliminateCard = eliminateCard; // Removed direct access
window.resolveDraw = resolveDraw;
window.callCambio = callCambio;
window.copyRoomId = copyRoomId;
window.startGame = startGame;
window.playAgain = playAgain;
window.skipAbility = skipAbility;
window.toggleAdminMode = toggleAdminMode;
