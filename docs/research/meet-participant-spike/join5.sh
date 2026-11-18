#!/bin/bash
# join5.sh <binary> <mode> <name> <typed name>  (THROWAWAY SPIKE)
# join4 plus: accept a dialog that offers "Join now" after the first click
# (Gemini notes, recording), then confirm from the viewer Koli that the new
# participant is really seen, and measure a minute of CPU and memory.
cd "$(dirname "$0")"
(node t2-meet.mjs "$1" "$2" "$3" > "t2-$3.log" 2>&1 &)
sleep 7; cat "t2-$3.log"; P=$(cat "t2-$3.port"); V=$(cat t2-human.port)
val(){ curl -s -X POST "http://127.0.0.1:$1/eval" --data-binary "$2" | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{const j=JSON.parse(s);console.log(j.value!==undefined?j.value:'ERR '+j.error)})"; }
box(){ val $P "(()=>{const i=document.querySelector('input[aria-label=\"Your name\"]'); if(!i) return ''; const r=i.getBoundingClientRect(); if(r.width<50) return ''; return 'x='+Math.round(r.x+40)+'&y='+Math.round(r.y+r.height/2)})()"; }
btn(){ val $P "(()=>{const b=[...document.querySelectorAll('button,[role=button]')].find(b=>$1); if(!b) return ''; const r=b.getBoundingClientRect(); return r.width? 'x='+Math.round(r.x+r.width/2)+'&y='+Math.round(r.y+r.height/2):''})()"; }
curl -s "http://127.0.0.1:$P/goto?url=https://meet.google.com/abc-defg-hij" >/dev/null
for i in $(seq 1 25); do sleep 1; R=$(box); [ -n "$R" ] && break; done
[ -z "$R" ] && { echo "no name box"; val $P "document.body.innerText.replace(/\n+/g,' | ').slice(0,300)"; exit 0; }
sleep 3
G=$(btn "b.innerText.trim()==='Got it'"); [ -n "$G" ] && { curl -s "http://127.0.0.1:$P/hclick?$G" >/dev/null; sleep 2; }
R=$(box); curl -s "http://127.0.0.1:$P/hclick?$R" >/dev/null; sleep 1
curl -s -X POST "http://127.0.0.1:$P/type" --data-binary "${4:-Koli}" >/dev/null; sleep 2
J=$(btn "/Join now|Ask to join/.test(b.innerText) && !b.disabled"); echo "join button: ${J:-none or disabled}"
[ -z "$J" ] && exit 0
curl -s "http://127.0.0.1:$P/hclick?$J" >/dev/null; sleep 8
D=$(btn "b.closest('[role=dialog]') && b.innerText.trim()==='Join now'"); [ -n "$D" ] && { echo "dialog: pressing Join now"; curl -s "http://127.0.0.1:$P/hclick?$D" >/dev/null; sleep 6; }
echo "own page: $(val $P "JSON.stringify({ua: navigator.userAgent.match(/Chrome\/[\d.]+/)[0], refused: /can't join/.test(document.body.innerText), inCall: !!document.querySelector('[aria-label*=\"Leave call\"]')})")"
curl -s "http://127.0.0.1:$P/stats" >/dev/null; sleep 60
echo "cost: $(curl -s "http://127.0.0.1:$P/stats")"
echo "sends: $(val $P "window.__koli.sending().then(r=>JSON.stringify(r.filter(x=>x.active&&x.width).map(x=>x.width+'x'+x.height+'@'+x.fps+' sent '+x.sent)))")"
echo "viewer sees: $(val $V "JSON.stringify([...document.querySelectorAll('[data-participant-id]')].map(t=>(t.querySelector('span.notranslate')||{}).innerText))")"
