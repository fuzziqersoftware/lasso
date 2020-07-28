'use strict';

import * as page from './page.js';
import * as ws from './websocket.js';
import client_context from './client_context.js';

let name_input = null;
let box_elem = null;
let instructions_elem = null
let viewport = null;

export function create(viewport_elem) {
  viewport = viewport_elem;
  name_input = page.input({class: 'form_input', placeholder: 'Name'});

  box_elem = page.div({class: 'login_box'}, [
    page.text("Enter your name"),
    page.br(),
    page.span({class: 'subtext'}, "or leave blank and press Enter to watch!"),
    page.br(),
    name_input,
  ]);
  page.add(box_elem);

  instructions_elem = page.div({class: 'instructions_box'}, [
    page.span({class: 'game_title'}, 'Lasso'),
    page.br(),
    page.br(),
    page.text('Make loops around objects and other players, but don\'t touch them!'),
    page.br(),
    page.text('Your tail will break if you move too fast.'),
  ]);
  page.add(instructions_elem);

  name_input.focus();

  document.onkeypress = on_input_key_press;
}

export function destroy() {
  page.remove_from_root(box_elem);
  page.remove_from_root(instructions_elem);
  viewport.style.display = 'inline-block';
  name_input = null;
  box_elem = null;
  instructions_elem = null;
  viewport = null;
  document.onkeypress = null;
}

function on_input_key_press(event) {
  if (event.keyCode === 13) {
    event.preventDefault();
    if (name_input.value.length === 0) {
      client_context.name = '';
      ws.send_message({command: 'register_watcher'});
      destroy();
    } else {
      client_context.name = name_input.value;
      ws.send_message({
        command: 'register_player',
        name: name_input.value,
      });
      destroy();
    }
  }
}
