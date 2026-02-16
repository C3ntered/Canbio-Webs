import re

with open('Frontend/bridge.js', 'r') as f:
    content = f.read()

# Locate the block where elements are shown/hidden
# const discardPileContainer = document.getElementById('discard-pile');
# const myHandContainer = document.getElementById('my-hand');
# const opponentsHandsContainer = document.getElementById('opponents-hands');
# const actionButtons = document.querySelector('.action-buttons');

old_selectors = """    const discardPileContainer = document.getElementById('discard-pile');
    const myHandContainer = document.getElementById('my-hand');
    const opponentsHandsContainer = document.getElementById('opponents-hands');
    const actionButtons = document.querySelector('.action-buttons');"""

new_selectors = """    const discardPileContainer = document.getElementById('discard-pile');
    const deckPileContainer = document.getElementById('deck-pile');
    const cambioContainer = document.getElementById('cambio-container');
    const myHandContainer = document.getElementById('my-hand');
    const opponentsHandsContainer = document.getElementById('opponents-hands');
    const actionButtons = document.querySelector('.action-buttons');"""

content = content.replace(old_selectors, new_selectors)

# Update visibility logic
# if (isPlaying) {
#     if (discardPileContainer) discardPileContainer.style.display = isViewingPhase ? 'none' : 'block';
#     if (myHandContainer) myHandContainer.style.display = 'block';
#     if (opponentsHandsContainer) opponentsHandsContainer.style.display = isViewingPhase ? 'none' : 'block';
#     if (actionButtons) actionButtons.style.display = isViewingPhase ? 'none' : 'flex';

old_visible = """    if (isPlaying) {
        if (discardPileContainer) discardPileContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (myHandContainer) myHandContainer.style.display = 'block';
        if (opponentsHandsContainer) opponentsHandsContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (actionButtons) actionButtons.style.display = isViewingPhase ? 'none' : 'flex';"""

new_visible = """    if (isPlaying) {
        if (discardPileContainer) discardPileContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (deckPileContainer) deckPileContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (cambioContainer) cambioContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (myHandContainer) myHandContainer.style.display = 'block';
        if (opponentsHandsContainer) opponentsHandsContainer.style.display = isViewingPhase ? 'none' : 'block';
        if (actionButtons) actionButtons.style.display = isViewingPhase ? 'none' : 'flex';"""

content = content.replace(old_visible, new_visible)

# Update hidden logic
# } else {
#     if (discardPileContainer) discardPileContainer.style.display = 'none';
#     if (myHandContainer) myHandContainer.style.display = 'none';
#     if (opponentsHandsContainer) opponentsHandsContainer.style.display = 'none';
# }

old_hidden = """    } else {
        if (discardPileContainer) discardPileContainer.style.display = 'none';
        if (myHandContainer) myHandContainer.style.display = 'none';
        if (opponentsHandsContainer) opponentsHandsContainer.style.display = 'none';
    }"""

new_hidden = """    } else {
        if (discardPileContainer) discardPileContainer.style.display = 'none';
        if (deckPileContainer) deckPileContainer.style.display = 'none';
        if (cambioContainer) cambioContainer.style.display = 'none';
        if (myHandContainer) myHandContainer.style.display = 'none';
        if (opponentsHandsContainer) opponentsHandsContainer.style.display = 'none';
    }"""

content = content.replace(old_hidden, new_hidden)

with open('Frontend/bridge.js', 'w') as f:
    f.write(content)
