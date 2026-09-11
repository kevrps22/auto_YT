import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';

import 'main.dart' show Cfg, runPython;

/// Studio personnage : generer / importer une mascotte, placer sa machoire
/// et sa bouche, regler taille et position, choisir celle qui parle dans les videos.
class CharacterTab extends StatefulWidget {
  const CharacterTab({super.key});
  @override
  State<CharacterTab> createState() => _CharacterTabState();
}

class _CharacterTabState extends State<CharacterTab> {
  List<_Char> _chars = [];
  _Char? _sel;
  String? _active;
  bool _busy = false;
  String _log = '';
  int _imgVersion = 0; // force le rechargement de l'image apres regeneration

  Directory get _dir => Directory('${Cfg.botDir}\\characters');

  @override
  void initState() {
    super.initState();
    _reload();
  }

  // ---------------------------------------------------------------- donnees
  void _reload({String? select}) {
    final list = <_Char>[];
    if (_dir.existsSync()) {
      for (final e in _dir.listSync()) {
        if (e is! Directory) continue;
        final f = File('${e.path}\\char.json');
        if (!f.existsSync()) continue;
        try {
          final m = jsonDecode(f.readAsStringSync()) as Map<String, dynamic>;
          list.add(_Char(e.path.split(Platform.pathSeparator).last, m));
        } catch (_) {}
      }
    }
    list.sort((a, b) => a.name.toLowerCase().compareTo(b.name.toLowerCase()));
    final af = File('${_dir.path}\\active.txt');
    final active = af.existsSync() ? af.readAsStringSync().trim() : null;
    final want = select ?? _sel?.slug;
    setState(() {
      _chars = list;
      _active = active;
      _sel = list.where((c) => c.slug == want).firstOrNull ??
          (list.isNotEmpty ? list.first : null);
      _imgVersion++;
    });
  }

  void _save(_Char c) {
    File('${_dir.path}\\${c.slug}\\char.json')
        .writeAsStringSync(const JsonEncoder.withIndent('  ').convert(c.data));
  }

  Future<void> _run(List<String> args, {String? select}) async {
    setState(() {
      _busy = true;
      _log = '';
    });
    String slug = '';
    await for (final line in runPython(args)) {
      if (line.contains('SLUG=')) {
        slug = RegExp(r'SLUG=(\S+)').firstMatch(line)?.group(1) ?? '';
      }
      setState(() => _log += line);
    }
    // vide le cache image sinon Flutter reaffiche l'ancienne version
    imageCache.clear();
    imageCache.clearLiveImages();
    setState(() => _busy = false);
    _reload(select: select ?? (slug.isNotEmpty ? slug : null));
  }

  // ---------------------------------------------------------------- actions
  Future<void> _generate() async {
    final res = await showDialog<List<String>>(
        context: context, builder: (_) => const _GenerateDialog());
    if (res == null) return;
    await _run(['character.py', '--generate', res[0], '--name', res[1]]);
  }

  Future<void> _import() async {
    // Boite de dialogue Windows native (evite une dependance supplementaire)
    const ps = r'''
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Filter = "Images|*.png;*.jpg;*.jpeg;*.webp|Tous|*.*"
if ($d.ShowDialog() -eq "OK") { Write-Output $d.FileName }
''';
    final r = await Process.run(
        'powershell', ['-NoProfile', '-STA', '-Command', ps]);
    final path = r.stdout.toString().trim();
    if (path.isEmpty) return;
    final name = path.split(Platform.pathSeparator).last.split('.').first;
    await _run(['character.py', '--import', path, '--name', name]);
  }

  Future<void> _delete(_Char c) async {
    final ok = await showDialog<bool>(
        context: context,
        builder: (ctx) => AlertDialog(
              title: Text('Supprimer « ${c.name} » ?'),
              content: const Text('Le dossier du personnage sera efface.'),
              actions: [
                TextButton(
                    onPressed: () => Navigator.pop(ctx, false),
                    child: const Text('Annuler')),
                FilledButton(
                    onPressed: () => Navigator.pop(ctx, true),
                    child: const Text('Supprimer')),
              ],
            ));
    if (ok != true) return;
    Directory('${_dir.path}\\${c.slug}').deleteSync(recursive: true);
    setState(() => _sel = null);
    _reload();
  }

  // ---------------------------------------------------------------- vue
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(16),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(width: 230, child: _list()),
        const SizedBox(width: 16),
        Expanded(child: _sel == null ? _empty() : _editor(_sel!)),
      ]),
    );
  }

  Widget _empty() => Center(
        child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
          const Icon(Icons.face_retouching_natural, size: 64, color: Colors.white24),
          const SizedBox(height: 12),
          const Text('Aucun personnage.', style: TextStyle(color: Colors.white54)),
          const SizedBox(height: 16),
          FilledButton.icon(
              onPressed: _busy ? null : _generate,
              icon: const Icon(Icons.auto_awesome),
              label: const Text('Generer un personnage')),
        ]),
      );

  Widget _list() {
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      FilledButton.icon(
          onPressed: _busy ? null : _generate,
          icon: const Icon(Icons.auto_awesome, size: 18),
          label: const Text('Generer')),
      const SizedBox(height: 8),
      OutlinedButton.icon(
          onPressed: _busy ? null : _import,
          icon: const Icon(Icons.upload_file, size: 18),
          label: const Text('Importer')),
      const Divider(height: 24),
      Expanded(
        child: ListView.builder(
          itemCount: _chars.length,
          itemBuilder: (_, i) {
            final c = _chars[i];
            final isActive = c.slug == _active;
            final cut = File('${_dir.path}\\${c.slug}\\cutout.png');
            return Card(
              color: c.slug == _sel?.slug ? Colors.white10 : Colors.transparent,
              elevation: 0,
              child: ListTile(
                dense: true,
                leading: SizedBox(
                  width: 34,
                  height: 34,
                  child: cut.existsSync()
                      ? Image.file(cut, key: ValueKey('${c.slug}$_imgVersion'))
                      : const Icon(Icons.person),
                ),
                title: Text(c.name, overflow: TextOverflow.ellipsis),
                subtitle: isActive
                    ? const Text('actif', style: TextStyle(color: Color(0xFF4CAF50), fontSize: 11))
                    : null,
                onTap: () => setState(() => _sel = c),
              ),
            );
          },
        ),
      ),
      if (_busy) const LinearProgressIndicator(),
      if (_log.isNotEmpty)
        Container(
          height: 90,
          margin: const EdgeInsets.only(top: 8),
          padding: const EdgeInsets.all(6),
          decoration: BoxDecoration(
              color: Colors.black45, borderRadius: BorderRadius.circular(6)),
          child: SingleChildScrollView(
            reverse: true,
            child: Text(_log,
                style: const TextStyle(fontFamily: 'monospace', fontSize: 10)),
          ),
        ),
    ]);
  }

  Widget _editor(_Char c) {
    final isActive = c.slug == _active;
    return SingleChildScrollView(
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Expanded(
            child: Text(c.name,
                style: const TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
          ),
          if (isActive)
            const Chip(
                label: Text('Personnage actif'),
                avatar: Icon(Icons.check_circle, size: 16, color: Color(0xFF4CAF50))),
          if (!isActive)
            FilledButton.icon(
              onPressed: _busy
                  ? null
                  : () async {
                      File('${_dir.path}\\active.txt').writeAsStringSync(c.slug);
                      _reload(select: c.slug);
                    },
              icon: const Icon(Icons.star, size: 18),
              label: const Text('Definir comme actif'),
            ),
          IconButton(
              onPressed: _busy ? null : () => _delete(c),
              icon: const Icon(Icons.delete_outline),
              tooltip: 'Supprimer'),
        ]),
        const SizedBox(height: 4),
        const Text(
            'Glisse la zone CYAN sur la bouche du perso ; les poignees rondes en reglent la largeur. '
            'Elle montre la bouche a son ouverture maximale.',
            style: TextStyle(color: Colors.white54, fontSize: 12)),
        const SizedBox(height: 12),
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _MouthEditor(
            key: ValueKey('${c.slug}$_imgVersion'),
            image: File('${_dir.path}\\${c.slug}\\cutout.png'),
            data: c.data,
            onChanged: () => setState(() {}),
            onCommit: () => _save(c),
          ),
          const SizedBox(width: 20),
          Expanded(child: _settings(c)),
        ]),
      ]),
    );
  }

  Widget _settings(_Char c) {
    Widget slider(String label, String key, double min, double max,
        {String Function(double)? fmt}) {
      final v = (c.data[key] as num?)?.toDouble() ?? min;
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Text('$label : ${fmt != null ? fmt(v) : v.toStringAsFixed(3)}',
            style: const TextStyle(fontSize: 12)),
        Slider(
          value: v.clamp(min, max),
          min: min,
          max: max,
          onChanged: (x) => setState(() => c.data[key] = x),
          onChangeEnd: (_) => _save(c),
        ),
      ]);
    }

    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      slider('Taille dans la video', 'width', 0.12, 0.55,
          fmt: (v) => '${(v * 100).round()} % de la largeur'),
      slider('Ouverture max de la bouche', 'open', 0.01, 0.16),
      slider('Dandinement', 'bob', 0, 20, fmt: (v) => '${v.round()} px'),
      slider('Marge aux bords', 'margin', 0.0, 0.12,
          fmt: (v) => '${(v * 100).toStringAsFixed(1)} %'),
      const SizedBox(height: 8),
      DropdownButtonFormField<String>(
        initialValue: (c.data['anchor'] as String?) ?? 'bottom-left',
        decoration: const InputDecoration(labelText: 'Position', isDense: true),
        items: const [
          DropdownMenuItem(value: 'bottom-left', child: Text('En bas a gauche')),
          DropdownMenuItem(value: 'bottom-center', child: Text('En bas au centre')),
          DropdownMenuItem(value: 'bottom-right', child: Text('En bas a droite')),
        ],
        onChanged: (v) {
          if (v == null) return;
          setState(() => c.data['anchor'] = v);
          _save(c);
        },
      ),
      const SizedBox(height: 8),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        dense: true,
        title: const Text('Incruster dans les videos', style: TextStyle(fontSize: 13)),
        value: c.data['enabled'] != false,
        onChanged: (v) {
          setState(() => c.data['enabled'] = v);
          _save(c);
        },
      ),
      SwitchListTile(
        contentPadding: EdgeInsets.zero,
        dense: true,
        title: const Text('Seulement apres le hook', style: TextStyle(fontSize: 13)),
        subtitle: const Text('Laisse les 3 premieres secondes aux sous-titres',
            style: TextStyle(fontSize: 11, color: Colors.white38)),
        value: c.data['after_hook'] != false,
        onChanged: (v) {
          setState(() => c.data['after_hook'] = v);
          _save(c);
        },
      ),
      const Divider(height: 24),
      TextFormField(
        initialValue: (c.data['prompt'] as String?) ?? '',
        maxLines: 3,
        decoration: const InputDecoration(
          labelText: 'Description (prompt) du personnage',
          hintText: 'ex : un raton laveur hyperactif en survetement fluo',
          isDense: true,
        ),
        onChanged: (v) => c.data['prompt'] = v,
        onFieldSubmitted: (_) => _save(c),
      ),
      const SizedBox(height: 8),
      Wrap(spacing: 8, runSpacing: 8, children: [
        OutlinedButton.icon(
          onPressed: _busy
              ? null
              : () {
                  _save(c);
                  final p = (c.data['prompt'] as String?) ?? '';
                  if (p.trim().isEmpty) return;
                  _run(['character.py', '--generate', p, '--name', c.name],
                      select: c.slug);
                },
          icon: const Icon(Icons.refresh, size: 18),
          label: const Text('Regenerer l\'image'),
        ),
        OutlinedButton.icon(
          onPressed:
              _busy ? null : () => _run(['character.py', '--prepare', c.slug], select: c.slug),
          icon: const Icon(Icons.auto_fix_high, size: 18),
          label: const Text('Re-detourer'),
        ),
        OutlinedButton.icon(
          onPressed: _busy
              ? null
              : () {
                  _save(c);
                  _run(['character.py', '--preview', c.slug], select: c.slug);
                },
          icon: const Icon(Icons.visibility, size: 18),
          label: const Text('Apercu bouche ouverte'),
        ),
      ]),
      const SizedBox(height: 12),
      Builder(builder: (_) {
        final f = File('${_dir.path}\\${c.slug}\\preview_open.png');
        if (!f.existsSync()) return const SizedBox.shrink();
        return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Apercu bouche ouverte',
              style: TextStyle(fontSize: 12, color: Colors.white54)),
          const SizedBox(height: 6),
          Container(
            padding: const EdgeInsets.all(8),
            decoration: BoxDecoration(
                color: Colors.black26, borderRadius: BorderRadius.circular(8)),
            child: Image.file(f,
                height: 190, key: ValueKey('prev${c.slug}$_imgVersion')),
          ),
        ]);
      }),
    ]);
  }
}

// ------------------------------------------------------------------ modele
class _Char {
  _Char(this.slug, this.data);
  final String slug;
  final Map<String, dynamic> data;
  String get name => (data['name'] as String?) ?? slug;
}

extension _FirstOrNull<T> on Iterable<T> {
  T? get firstOrNull => isEmpty ? null : first;
}

// ------------------------------------------------------------------ dialogue
class _GenerateDialog extends StatefulWidget {
  const _GenerateDialog();
  @override
  State<_GenerateDialog> createState() => _GenerateDialogState();
}

class _GenerateDialogState extends State<_GenerateDialog> {
  final _prompt = TextEditingController(
      text: 'cute 3d cartoon mascot, big round head, huge expressive eyes, pixar style');
  final _name = TextEditingController(text: 'Mascotte');

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Generer un personnage'),
      content: SizedBox(
        width: 460,
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(
            controller: _name,
            decoration: const InputDecoration(labelText: 'Nom'),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _prompt,
            maxLines: 4,
            decoration: const InputDecoration(
              labelText: 'Description (prompt)',
              helperText: 'Le style « bouche fermee, face, fond uni » est ajoute automatiquement.',
            ),
          ),
        ]),
      ),
      actions: [
        TextButton(
            onPressed: () => Navigator.pop(context), child: const Text('Annuler')),
        FilledButton(
          onPressed: () => Navigator.pop(
              context, [_prompt.text.trim(), _name.text.trim()]),
          child: const Text('Generer'),
        ),
      ],
    );
  }
}

// ------------------------------------------------------------------ editeur bouche
/// Affiche le personnage detoure avec la ligne de machoire et les bords de bouche,
/// tous deplacables a la souris.
class _MouthEditor extends StatefulWidget {
  const _MouthEditor({
    super.key,
    required this.image,
    required this.data,
    required this.onChanged,
    required this.onCommit,
  });

  final File image;
  final Map<String, dynamic> data;
  final VoidCallback onChanged;
  final VoidCallback onCommit;

  @override
  State<_MouthEditor> createState() => _MouthEditorState();
}

class _MouthEditorState extends State<_MouthEditor> {
  Size? _intrinsic;
  String? _drag; // 'jaw' | 'x0' | 'x1'

  static const double _boxW = 320;
  static const double _boxH = 420;

  @override
  void initState() {
    super.initState();
    _measure();
  }

  Future<void> _measure() async {
    if (!widget.image.existsSync()) return;
    final bytes = await widget.image.readAsBytes();
    final codec = await ui.instantiateImageCodec(bytes);
    final frame = await codec.getNextFrame();
    if (!mounted) return;
    setState(() => _intrinsic =
        Size(frame.image.width.toDouble(), frame.image.height.toDouble()));
  }

  double _d(String k, double fallback) =>
      (widget.data[k] as num?)?.toDouble() ?? fallback;

  @override
  Widget build(BuildContext context) {
    if (!widget.image.existsSync()) {
      return const SizedBox(
        width: _boxW,
        height: _boxH,
        child: Center(child: Text('Image manquante', style: TextStyle(color: Colors.white38))),
      );
    }
    final iw = _intrinsic?.width ?? 1;
    final ih = _intrinsic?.height ?? 1;
    final scale = (_boxW / iw) < (_boxH / ih) ? _boxW / iw : _boxH / ih;
    final dw = iw * scale, dh = ih * scale;
    final dx = (_boxW - dw) / 2, dy = (_boxH - dh) / 2;

    // compat : les anciens personnages stockaient la position sous la cle "jaw"
    final my = _d('mouth_y', _d('jaw', 0.60));
    final x0 = _d('mouth_x0', 0.32);
    final x1 = _d('mouth_x1', 0.68);
    final open = _d('open', 0.055);

    void update(Offset local) {
      final nx = ((local.dx - dx) / dw).clamp(0.0, 1.0);
      final ny = ((local.dy - dy) / dh).clamp(0.0, 1.0);
      switch (_drag) {
        case 'x0':
          widget.data['mouth_x0'] = nx.clamp(0.0, _d('mouth_x1', 0.68) - 0.02);
          break;
        case 'x1':
          widget.data['mouth_x1'] = nx.clamp(_d('mouth_x0', 0.32) + 0.02, 1.0);
          break;
        default: // deplace la bouche entiere
          final half = (x1 - x0) / 2;
          widget.data['mouth_y'] = ny;
          widget.data['mouth_x0'] = (nx - half).clamp(0.0, 1.0 - 2 * half);
          widget.data['mouth_x1'] = (nx + half).clamp(2 * half, 1.0);
      }
      widget.onChanged();
    }

    // poignee de bord si on clique dessus, sinon deplacement de la bouche
    void pick(Offset local) {
      final nx = (local.dx - dx) / dw;
      final near = (nx - x0).abs() < (nx - x1).abs() ? 'x0' : 'x1';
      final dX = (nx - (near == 'x0' ? x0 : x1)).abs();
      _drag = dX < 0.045 ? near : 'move';
    }

    return SizedBox(
      width: _boxW,
      height: _boxH,
      child: GestureDetector(
        onPanStart: (d) {
          pick(d.localPosition);
          update(d.localPosition);
        },
        onPanUpdate: (d) => update(d.localPosition),
        onPanEnd: (_) {
          _drag = null;
          widget.onCommit();
        },
        child: Container(
          decoration: BoxDecoration(
            color: Colors.black26,
            borderRadius: BorderRadius.circular(10),
            border: Border.all(color: Colors.white12),
          ),
          child: Stack(children: [
            Positioned(
              left: dx,
              top: dy,
              width: dw,
              height: dh,
              child: Image.file(widget.image, fit: BoxFit.fill),
            ),
            // la bouche telle qu'elle s'ouvrira (ellipse a l'ouverture max)
            Positioned(
              left: dx + x0 * dw,
              top: dy + my * dh - open * dh / 2,
              width: (x1 - x0) * dw,
              height: open * dh,
              child: Container(
                decoration: BoxDecoration(
                  color: const Color(0x5500E5FF),
                  shape: BoxShape.rectangle,
                  borderRadius: BorderRadius.all(
                      Radius.elliptical((x1 - x0) * dw / 2, open * dh / 2)),
                  border: Border.all(color: const Color(0xFF00E5FF), width: 1.5),
                ),
              ),
            ),
            // poignees de largeur
            for (final h in [x0, x1])
              Positioned(
                left: dx + h * dw - 6,
                top: dy + my * dh - 6,
                child: Container(
                  width: 12,
                  height: 12,
                  decoration: const BoxDecoration(
                      color: Color(0xFF00E5FF), shape: BoxShape.circle),
                ),
              ),
          ]),
        ),
      ),
    );
  }
}
