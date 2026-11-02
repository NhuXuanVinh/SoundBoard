"""Portable JSON library; files remain at their original locations."""
import json
from pathlib import Path
import uuid


DATA_DIR = Path(__file__).resolve().parent.parent / 'data'


def new_clip(path, duration):
    return dict(id=uuid.uuid4().hex, name=Path(path).stem, path=str(Path(path).resolve()),
                duration=float(duration), gain=1.0, loop=False)


def load_state(path):
    if not path.exists():
        return {}, None
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or not isinstance(state.get('clips', []), list):
            raise ValueError('Invalid library format')
        seen = set()
        for clip in state.get('clips', []):
            if not isinstance(clip, dict):
                raise ValueError('Invalid sound entry')
            for key in ('id', 'path', 'name'):
                if not isinstance(clip.get(key), str) or not clip[key]:
                    raise ValueError('Invalid sound entry')
            if clip['id'] in seen:
                raise ValueError('Duplicate sound identifier')
            seen.add(clip['id'])
            clip['gain'] = max(0, min(1.5, float(clip.get('gain', 1))))
            clip['duration'] = max(0, float(clip.get('duration', 0)))
            clip['loop'] = bool(clip.get('loop', False))
        return state, None
    except (OSError, ValueError, TypeError) as exc:
        # Preserve the damaged file before a future save replaces the library.
        backup = path.with_name(f'{path.stem}.recovery-{uuid.uuid4().hex[:8]}.json')
        try:
            backup.write_bytes(path.read_bytes())
            return {}, f'Could not read the library: {exc}. Original saved to {backup.name}.'
        except OSError:
            return {}, f'Could not read or back up the library: {exc}.'


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)
