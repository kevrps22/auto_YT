import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

import 'character_tab.dart';

void main() => runApp(const YtStudioApp());

// ------------------------------------------------------------------ config
class Cfg {
  static String botDir = r'C:\Users\kevin\Documents\yt-shorts-bot';
  static String python = 'python';
  static String ffmpegDir =
      r'C:\Users\kevin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin';
  static String espeakDir = r'C:\Program Files\eSpeak NG'; // pour Kokoro (voix FR)

  static File get _file =>
      File('${Platform.environment['USERPROFILE'] ?? '.'}\\.yt_studio.json');

  static Future<void> load() async {
    try {
      if (_file.existsSync()) {
        final m = jsonDecode(_file.readAsStringSync()) as Map<String, dynamic>;
        botDir = m['botDir'] ?? botDir;
        python = m['python'] ?? python;
        ffmpegDir = m['ffmpegDir'] ?? ffmpegDir;
        espeakDir = m['espeakDir'] ?? espeakDir;
      }
    } catch (_) {}
  }

  static Future<void> save() async {
    try {
      _file.writeAsStringSync(jsonEncode(
          {'botDir': botDir, 'python': python, 'ffmpegDir': ffmpegDir, 'espeakDir': espeakDir}));
    } catch (_) {}
  }

  /// Lit le fichier .env du projet Python -> map cle/valeur.
  static Map<String, String> env() {
    final f = File('$botDir${Platform.pathSeparator}.env');
    final out = <String, String>{};
    if (f.existsSync()) {
      for (final line in f.readAsLinesSync()) {
        final l = line.trim();
        if (l.isEmpty || l.startsWith('#') || !l.contains('=')) continue;
        final i = l.indexOf('=');
        out[l.substring(0, i).trim()] =
            l.substring(i + 1).trim().replaceAll('"', '').replaceAll("'", '');
      }
    }
    return out;
  }
}

/// Ouvre l'explorateur sur le DOSSIER de la video, fichier selectionne si possible.
/// `explorer /select,` echoue silencieusement quand le nom contient des espaces,
/// '#' ou '...' : on passe par le COM Shell de Windows (PowerShell), qui gere
/// n'importe quel chemin, avec repli sur l'ouverture simple du dossier.
Future<void> revealInExplorer(File f) async {
  if (!f.existsSync()) return;
  final dir = f.parent.absolute.path;
  try {
    // Le chemin est injecte dans la commande PowerShell (quotes simples echappees) :
    // c'est la seule forme qui survit a la fois a l'echappement Dart et a explorer.
    final safe = f.absolute.path.replaceAll("'", "''");
    final ps = "Start-Process explorer.exe -ArgumentList '/select,\"$safe\"'";
    final r0 = await Process.run(
      'powershell',
      ['-NoProfile', '-NonInteractive', '-Command', ps],
    );
    if (r0.exitCode == 0) return;
  } catch (_) {}
  await Process.run('explorer.exe', [dir]); // repli : au moins le bon dossier
}

/// Lance un script Python du projet en streamant les logs. FFmpeg injecte au PATH.
Stream<String> runPython(List<String> args, {Map<String, String>? extraEnv}) async* {
  final env = <String, String>{
    ...Platform.environment,
    'PATH': '${Cfg.ffmpegDir};${Cfg.espeakDir};${Platform.environment['PATH'] ?? ''}',
    'PYTHONIOENCODING': 'utf-8',
    'PHONEMIZER_ESPEAK_LIBRARY': '${Cfg.espeakDir}\\libespeak-ng.dll',
    ...?extraEnv,
  };
  // -u : sortie Python NON bufferisee -> les logs arrivent en direct, pas d'un bloc
  final proc = await Process.start(Cfg.python, ['-u', ...args],
      workingDirectory: Cfg.botDir, environment: env);
  final ctrl = StreamController<String>();
  proc.stdout.transform(const Utf8Decoder(allowMalformed: true)).listen(ctrl.add);
  proc.stderr.transform(const Utf8Decoder(allowMalformed: true)).listen(ctrl.add);
  proc.exitCode.then((c) {
    ctrl.add('\n[Termine — code $c]');
    ctrl.close();
  });
  yield* ctrl.stream;
}

// ------------------------------------------------------------------ app
class YtStudioApp extends StatelessWidget {
  const YtStudioApp({super.key});
  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'YT Studio',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        brightness: Brightness.dark,
        colorSchemeSeed: const Color(0xFFFF0033),
        scaffoldBackgroundColor: const Color(0xFF0F0F0F),
      ),
      home: const Home(),
    );
  }
}

class Home extends StatefulWidget {
  const Home({super.key});
  @override
  State<Home> createState() => _HomeState();
}

class _HomeState extends State<Home> {
  int _i = 0;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    Cfg.load().then((_) => setState(() => _ready = true));
  }

  @override
  Widget build(BuildContext context) {
    if (!_ready) return const Scaffold(body: Center(child: CircularProgressIndicator()));
    return Scaffold(
      body: Row(
        children: [
          NavigationRail(
            selectedIndex: _i,
            onDestinationSelected: (v) => setState(() => _i = v),
            labelType: NavigationRailLabelType.all,
            leading: Padding(
              padding: const EdgeInsets.only(top: 12, bottom: 8),
              child: IconButton(
                icon: const Icon(Icons.settings),
                tooltip: 'Reglages',
                onPressed: () => showDialog(context: context, builder: (_) => const SettingsDialog()),
              ),
            ),
            destinations: const [
              NavigationRailDestination(icon: Icon(Icons.movie_creation_outlined), label: Text('Generer')),
              NavigationRailDestination(icon: Icon(Icons.face_retouching_natural), label: Text('Perso')),
              NavigationRailDestination(icon: Icon(Icons.cloud_upload_outlined), label: Text('Upload')),
              NavigationRailDestination(icon: Icon(Icons.insights_outlined), label: Text('Stats')),
            ],
          ),
          const VerticalDivider(width: 1),
          Expanded(
            child: IndexedStack(
              index: _i,
              children: const [GenerateTab(), CharacterTab(), UploadTab(), StatsTab()],
            ),
          ),
        ],
      ),
    );
  }
}

// ------------------------------------------------------------------ Generer
class GenerateTab extends StatefulWidget {
  const GenerateTab({super.key});
  @override
  State<GenerateTab> createState() => _GenerateTabState();
}

class _GenerateTabState extends State<GenerateTab> {
  final _theme = TextEditingController();
  final _log = StringBuffer();
  final _scroll = ScrollController();
  bool _running = false;
  int _count = 1;
  Map<String, dynamic>? _meta;

  Future<void> _generate() async {
    setState(() {
      _running = true;
      _log.clear();
      _meta = null;
    });
    for (var i = 1; i <= _count; i++) {
      if (_count > 1) {
        setState(() => _log.write('\n═════ Vidéo $i/$_count ═════\n'));
      }
      final args = ['generate.py'];
      if (_theme.text.trim().isNotEmpty) args.add(_theme.text.trim());
      await for (final line in runPython(args)) {
        setState(() => _log.write(line));
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (_scroll.hasClients) _scroll.jumpTo(_scroll.position.maxScrollExtent);
        });
      }
    }
    final mf = File('${Cfg.botDir}\\output\\meta.json');
    if (mf.existsSync()) _meta = jsonDecode(mf.readAsStringSync());
    setState(() => _running = false);
  }

  void _openVideo() =>
      Process.run('cmd', ['/c', 'start', '', '${Cfg.botDir}\\output\\short.mp4']);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Generer une video', style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 16),
          Row(children: [
            Expanded(
              child: TextField(
                controller: _theme,
                decoration: const InputDecoration(
                  labelText: 'Theme (laisse vide = aleatoire)',
                  hintText: 'ex : les trous noirs',
                  border: OutlineInputBorder(),
                ),
              ),
            ),
            const SizedBox(width: 12),
            Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              const Text('Nombre', style: TextStyle(fontSize: 11, color: Colors.white54)),
              DropdownButton<int>(
                value: _count,
                items: [1, 2, 3, 5, 7, 10]
                    .map((n) => DropdownMenuItem(value: n, child: Text('$n')))
                    .toList(),
                onChanged: _running ? null : (v) => setState(() => _count = v!),
              ),
            ]),
            const SizedBox(width: 12),
            FilledButton.icon(
              onPressed: _running ? null : _generate,
              icon: _running
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.auto_awesome),
              label: Text(_running ? 'Generation…' : (_count > 1 ? 'Generer ($_count)' : 'Generer')),
            ),
          ]),
          const SizedBox(height: 16),
          Expanded(
            child: Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(color: const Color(0xFF161616), borderRadius: BorderRadius.circular(10)),
              child: SingleChildScrollView(
                controller: _scroll,
                child: Text(_log.isEmpty ? 'Les logs de generation apparaitront ici…' : _log.toString(),
                    style: const TextStyle(fontFamily: 'monospace', fontSize: 12.5, height: 1.4)),
              ),
            ),
          ),
          if (_meta != null) ...[
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(color: const Color(0xFF161616), borderRadius: BorderRadius.circular(10)),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(_meta!['title'] ?? '', style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 16)),
                const SizedBox(height: 6),
                Text(_meta!['description'] ?? '', maxLines: 3, overflow: TextOverflow.ellipsis,
                    style: const TextStyle(color: Colors.white70)),
                const SizedBox(height: 12),
                Row(children: [
                  OutlinedButton.icon(onPressed: _openVideo, icon: const Icon(Icons.play_arrow), label: const Text('Lire la video')),
                  const SizedBox(width: 10),
                  const Text('Prete a uploader (onglet Upload) →', style: TextStyle(color: Colors.white54)),
                ]),
              ]),
            ),
          ],
        ],
      ),
    );
  }
}

// ------------------------------------------------------------------ Upload (galerie)
class VideoItem {
  final Directory folder;
  final Map<String, dynamic> meta;
  VideoItem(this.folder, this.meta);
  String get title => meta['title'] ?? folder.path.split(Platform.pathSeparator).last;
  String get description => meta['description'] ?? '';
  List get tags => meta['tags'] ?? [];
  String get created => meta['created'] ?? '';
  String get tiktok =>
      meta['tiktok'] ??
      '${title.replaceAll(' #Shorts', '')} 👀🤯\n\n#pourtoi #fyp #lesaviezvous #culturegenerale #incroyable #apprendresurtiktok #wtf';
  File get poster => File('${folder.path}\\poster.jpg');

  /// Le fichier porte le titre de la video (ancien format : short.mp4).
  File get video {
    final named = File('${folder.path}\\${meta['file'] ?? 'short.mp4'}');
    if (named.existsSync()) return named;
    final mp4 = folder
        .listSync()
        .whereType<File>()
        .where((f) => f.path.toLowerCase().endsWith('.mp4'))
        .toList();
    return mp4.isNotEmpty ? mp4.first : named;
  }

  /// Score de viralite (Gemini). Si absent (anciennes videos), heuristique locale.
  int get virality {
    final v = meta['virality'];
    if (v is num) return v.toInt();
    final h = description.toLowerCase();
    var s = 45;
    if (RegExp(r'\b(ton|ta|tes|tu)\b').hasMatch(h)) s += 18;   // parle au spectateur
    if (RegExp(r'\d').hasMatch(h)) s += 10;                    // chiffre concret
    if (RegExp(r'tue|mort|mortel|danger|sang|corps|peau|cerveau|argent|arnaque|'
            r'jamais|faux|secret|invisible')
        .hasMatch(h)) s += 15;                                 // mots a forte charge
    return s.clamp(0, 100);
  }

  String get viralityReason => meta['virality_reason'] ?? '';
}

class UploadTab extends StatefulWidget {
  const UploadTab({super.key});
  @override
  State<UploadTab> createState() => _UploadTabState();
}

class _UploadTabState extends State<UploadTab> {
  List<VideoItem> _items = [];
  VideoItem? _sel;
  String _sort = 'virality'; // 'virality' | 'date'

  @override
  void initState() {
    super.initState();
    _scan();
  }

  void _scan() {
    final lib = Directory('${Cfg.botDir}\\output\\lib');
    final items = <VideoItem>[];
    if (lib.existsSync()) {
      for (final d in lib.listSync().whereType<Directory>()) {
        final mf = File('${d.path}\\meta.json');
        final hasVideo = d.listSync().whereType<File>().any((f) => f.path.toLowerCase().endsWith('.mp4'));
        if (mf.existsSync() && hasVideo) {
          try {
            items.add(VideoItem(d, jsonDecode(mf.readAsStringSync())));
          } catch (_) {}
        }
      }
    }
    _applySort(items);
    setState(() {
      _items = items;
      _sel = null;
    });
  }

  void _applySort(List<VideoItem> items) {
    if (_sort == 'virality') {
      items.sort((a, b) => b.virality.compareTo(a.virality));
    } else {
      items.sort((a, b) => b.folder.path.compareTo(a.folder.path)); // recent d'abord
    }
  }

  Color _viralColor(int v) => v >= 75
      ? const Color(0xFF4ADE80)          // vert : fort potentiel
      : v >= 55
          ? const Color(0xFFFFD400)      // jaune : correct
          : const Color(0xFFFF8A65);     // orange : faible

  Future<void> _delete(VideoItem it) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (_) => AlertDialog(
        title: const Text('Supprimer cette vidéo ?'),
        content: Text('« ${it.title} »\nCette action est définitive (fichier local).'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Annuler')),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: Colors.red),
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Supprimer'),
          ),
        ],
      ),
    );
    if (ok == true) {
      try {
        it.folder.deleteSync(recursive: true);
      } catch (_) {}
      _scan();
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(children: [
            Text('Mes videos generees', style: Theme.of(context).textTheme.headlineSmall),
            const Spacer(),
            const Text('Trier : ', style: TextStyle(color: Colors.white54)),
            DropdownButton<String>(
              value: _sort,
              items: const [
                DropdownMenuItem(value: 'virality', child: Text('🔥 Viralite')),
                DropdownMenuItem(value: 'date', child: Text('🕐 Plus recentes')),
              ],
              onChanged: (v) {
                setState(() => _sort = v!);
                final l = List<VideoItem>.from(_items);
                _applySort(l);
                setState(() => _items = l);
              },
            ),
            IconButton(onPressed: _scan, icon: const Icon(Icons.refresh), tooltip: 'Rafraichir'),
          ]),
          const SizedBox(height: 4),
          const Text('Clique une video, puis choisis Upload manuel (recommande) ou API.',
              style: TextStyle(color: Colors.white54)),
          const SizedBox(height: 16),
          Expanded(
            child: _items.isEmpty
                ? const Center(child: Text('Aucune video. Genere-en une dans l\'onglet Generer.',
                    style: TextStyle(color: Colors.white54)))
                : GridView.builder(
                    gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                      maxCrossAxisExtent: 220, childAspectRatio: 0.62, crossAxisSpacing: 12, mainAxisSpacing: 12),
                    itemCount: _items.length,
                    itemBuilder: (_, i) {
                      final it = _items[i];
                      final selected = it == _sel;
                      return GestureDetector(
                        onTap: () => setState(() => _sel = it),
                        child: Container(
                          decoration: BoxDecoration(
                            borderRadius: BorderRadius.circular(12),
                            border: Border.all(color: selected ? const Color(0xFFFF0033) : Colors.transparent, width: 3),
                            color: const Color(0xFF161616),
                          ),
                          clipBehavior: Clip.antiAlias,
                          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                            Expanded(
                              child: Stack(fit: StackFit.expand, children: [
                                it.poster.existsSync()
                                    ? Image.file(it.poster, fit: BoxFit.cover)
                                    : const ColoredBox(color: Color(0xFF222222), child: Icon(Icons.movie, size: 40)),
                                Positioned(
                                  top: 6, right: 6,
                                  child: Container(
                                    padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                                    decoration: BoxDecoration(
                                      color: _viralColor(it.virality),
                                      borderRadius: BorderRadius.circular(20),
                                    ),
                                    child: Text('${it.virality}',
                                        style: const TextStyle(
                                            fontSize: 12, fontWeight: FontWeight.bold, color: Colors.black)),
                                  ),
                                ),
                              ]),
                            ),
                            Padding(
                              padding: const EdgeInsets.all(8),
                              child: Text(it.title.replaceAll(' #Shorts', ''),
                                  maxLines: 2, overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w600)),
                            ),
                          ]),
                        ),
                      );
                    },
                  ),
          ),
          if (_sel != null) ...[
            const Divider(),
            Row(children: [
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [
                  Text(_sel!.title, maxLines: 1, overflow: TextOverflow.ellipsis,
                      style: const TextStyle(fontWeight: FontWeight.bold)),
                  Text(
                    '🔥 ${_sel!.virality}/100'
                    '${_sel!.viralityReason.isNotEmpty ? " — ${_sel!.viralityReason}" : ""}',
                    maxLines: 1, overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: 11.5, color: _viralColor(_sel!.virality)),
                  ),
                ]),
              ),
              const SizedBox(width: 8),
              IconButton(
                onPressed: () => _delete(_sel!),
                icon: const Icon(Icons.delete_outline),
                color: Colors.redAccent,
                tooltip: 'Supprimer',
              ),
              const SizedBox(width: 6),
              OutlinedButton.icon(
                onPressed: () => showDialog(context: context, builder: (_) => ManualUploadDialog(item: _sel!)),
                icon: const Icon(Icons.smart_display_outlined),
                label: const Text('YouTube (manuel)'),
              ),
              const SizedBox(width: 8),
              OutlinedButton.icon(
                onPressed: () => showDialog(context: context, builder: (_) => TikTokUploadDialog(item: _sel!)),
                icon: const Icon(Icons.music_note),
                label: const Text('TikTok'),
              ),
              const SizedBox(width: 8),
              FilledButton.icon(
                onPressed: () => showDialog(context: context, builder: (_) => ApiUploadDialog(item: _sel!)),
                icon: const Icon(Icons.cloud_upload),
                label: const Text('YT API'),
              ),
            ]),
          ],
        ],
      ),
    );
  }
}

// ---- champ copiable
class _CopyField extends StatelessWidget {
  final String label, value;
  final int maxLines;
  const _CopyField(this.label, this.value, {this.maxLines = 1});
  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Text(label, style: const TextStyle(color: Colors.white60, fontSize: 12)),
          const Spacer(),
          TextButton.icon(
            onPressed: () {
              Clipboard.setData(ClipboardData(text: value));
              ScaffoldMessenger.of(context).showSnackBar(
                  SnackBar(content: Text('$label copie'), duration: const Duration(seconds: 1)));
            },
            icon: const Icon(Icons.copy, size: 15),
            label: const Text('Copier'),
          ),
        ]),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(color: const Color(0xFF0F0F0F), borderRadius: BorderRadius.circular(8)),
          child: SelectableText(value, maxLines: maxLines > 1 ? null : 1, style: const TextStyle(fontSize: 13)),
        ),
      ]),
    );
  }
}

// ---- dialog upload manuel : champs copiables + ouverture fichier/YouTube
class ManualUploadDialog extends StatelessWidget {
  final VideoItem item;
  const ManualUploadDialog({super.key, required this.item});
  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Upload manuel (le plus safe)'),
      content: SizedBox(
        width: 600,
        child: SingleChildScrollView(
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            const Text('1. Ouvre la video et YouTube  2. Copie-colle le titre et la description',
                style: TextStyle(color: Colors.white54, fontSize: 12.5)),
            const SizedBox(height: 14),
            Wrap(spacing: 10, runSpacing: 8, children: [
              OutlinedButton.icon(
                onPressed: () => Process.run('cmd', ['/c', 'start', '', item.video.path]),
                icon: const Icon(Icons.play_arrow), label: const Text('Ouvrir la video')),
              OutlinedButton.icon(
                onPressed: () => revealInExplorer(item.video),
                icon: const Icon(Icons.folder_open), label: const Text('Afficher dans l\'explorateur')),
              OutlinedButton.icon(
                onPressed: () => Process.run('cmd', ['/c', 'start', '', 'https://youtube.com/upload']),
                icon: const Icon(Icons.open_in_new), label: const Text('Ouvrir YouTube')),
            ]),
            const SizedBox(height: 16),
            _CopyField('Titre', item.title),
            _CopyField('Description', item.description, maxLines: 6),
            _CopyField('Tags', item.tags.join(', ')),
          ]),
        ),
      ),
      actions: [TextButton(onPressed: () => Navigator.pop(context), child: const Text('Fermer'))],
    );
  }
}

// ---- dialog upload TikTok (manuel : legende + drag&drop + ouvrir TikTok)
class TikTokUploadDialog extends StatelessWidget {
  final VideoItem item;
  const TikTokUploadDialog({super.key, required this.item});
  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Upload TikTok (manuel)'),
      content: SizedBox(
        width: 600,
        child: SingleChildScrollView(
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            const Text('1. Ouvre TikTok + l\'explorateur  2. Glisse la video  3. Colle la legende',
                style: TextStyle(color: Colors.white54, fontSize: 12.5)),
            const SizedBox(height: 14),
            Wrap(spacing: 10, runSpacing: 8, children: [
              OutlinedButton.icon(
                onPressed: () => revealInExplorer(item.video),
                icon: const Icon(Icons.folder_open), label: const Text('Afficher dans l\'explorateur')),
              OutlinedButton.icon(
                onPressed: () => Process.run('cmd', ['/c', 'start', '', item.video.path]),
                icon: const Icon(Icons.play_arrow), label: const Text('Ouvrir la video')),
              OutlinedButton.icon(
                onPressed: () => Process.run('cmd', ['/c', 'start', '', 'https://www.tiktok.com/upload']),
                icon: const Icon(Icons.open_in_new), label: const Text('Ouvrir TikTok')),
            ]),
            const SizedBox(height: 16),
            _CopyField('Legende TikTok (+ hashtags)', item.tiktok, maxLines: 6),
          ]),
        ),
      ),
      actions: [TextButton(onPressed: () => Navigator.pop(context), child: const Text('Fermer'))],
    );
  }
}

// ---- dialog upload API : streaming des logs
class ApiUploadDialog extends StatefulWidget {
  final VideoItem item;
  const ApiUploadDialog({super.key, required this.item});
  @override
  State<ApiUploadDialog> createState() => _ApiUploadDialogState();
}

class _ApiUploadDialogState extends State<ApiUploadDialog> {
  final _log = StringBuffer();
  bool _running = false;
  String _privacy = 'public';
  String? _url;

  Future<void> _run() async {
    setState(() { _running = true; _log.clear(); _url = null; });
    await for (final line in runPython(['upload.py', widget.item.folder.path], extraEnv: {'YT_PRIVACY': _privacy})) {
      setState(() => _log.write(line));
      final m = RegExp(r'https?://youtu\.be/\S+').firstMatch(_log.toString());
      if (m != null) _url = m.group(0);
    }
    setState(() => _running = false);
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Upload API'),
      content: SizedBox(
        width: 600,
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Text(widget.item.title, style: const TextStyle(fontWeight: FontWeight.bold)),
          const SizedBox(height: 12),
          Row(children: [
            const Text('Visibilite : '),
            DropdownButton<String>(
              value: _privacy,
              items: const [
                DropdownMenuItem(value: 'public', child: Text('Publique')),
                DropdownMenuItem(value: 'unlisted', child: Text('Non repertoriee')),
                DropdownMenuItem(value: 'private', child: Text('Privee')),
              ],
              onChanged: _running ? null : (v) => setState(() => _privacy = v!),
            ),
            const Spacer(),
            FilledButton.icon(
              onPressed: _running ? null : _run,
              icon: _running
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                  : const Icon(Icons.cloud_upload),
              label: Text(_running ? 'Upload…' : 'Lancer'),
            ),
          ]),
          const SizedBox(height: 12),
          Container(
            height: 200, width: double.infinity, padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(color: const Color(0xFF0F0F0F), borderRadius: BorderRadius.circular(8)),
            child: SingleChildScrollView(
              child: Text(_log.isEmpty ? 'Logs…' : _log.toString(),
                  style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
            ),
          ),
          if (_url != null) ...[
            const SizedBox(height: 10),
            Row(children: [
              const Icon(Icons.check_circle, color: Colors.greenAccent, size: 18),
              const SizedBox(width: 8),
              Expanded(child: SelectableText(_url!)),
              OutlinedButton(onPressed: () => Process.run('cmd', ['/c', 'start', '', _url!]), child: const Text('Ouvrir')),
            ]),
          ],
        ]),
      ),
      actions: [TextButton(onPressed: () => Navigator.pop(context), child: const Text('Fermer'))],
    );
  }
}

// ------------------------------------------------------------------ Stats
class StatsTab extends StatefulWidget {
  const StatsTab({super.key});
  @override
  State<StatsTab> createState() => _StatsTabState();
}

class _StatsTabState extends State<StatsTab> {
  Map<String, dynamic>? _chan;
  List<dynamic> _videos = [];
  String? _error;
  DateTime? _updated;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _refresh();
    _timer = Timer.periodic(const Duration(seconds: 60), (_) => _refresh());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<dynamic> _api(String endpoint, Map<String, String> params, String key) async {
    params['key'] = key;
    final uri = Uri.https('www.googleapis.com', '/youtube/v3/$endpoint', params);
    final r = await http.get(uri);
    if (r.statusCode != 200) throw 'API ${r.statusCode}: ${r.body}';
    return jsonDecode(r.body);
  }

  Future<void> _refresh() async {
    try {
      final env = Cfg.env();
      final key = env['YT_API_KEY'];
      final ident = env['YT_CHANNEL'] ?? '@Attends_Quoi';
      if (key == null || key.isEmpty) throw 'YT_API_KEY absente du .env';

      final chanResp = ident.startsWith('UC')
          ? await _api('channels', {'part': 'snippet,statistics,contentDetails', 'id': ident}, key)
          : await _api('channels', {'part': 'snippet,statistics,contentDetails', 'forHandle': ident.replaceAll('@', '')}, key);
      final chan = chanResp['items'][0];
      final uploads = chan['contentDetails']['relatedPlaylists']['uploads'];

      final ids = <String>[];
      String? tok;
      do {
        final pl = await _api('playlistItems', {
          'part': 'contentDetails', 'playlistId': uploads, 'maxResults': '50',
          if (tok != null) 'pageToken': tok,
        }, key);
        for (final it in pl['items']) ids.add(it['contentDetails']['videoId']);
        tok = pl['nextPageToken'];
      } while (tok != null);

      final vids = <dynamic>[];
      for (var i = 0; i < ids.length; i += 50) {
        final chunk = ids.sublist(i, i + 50 > ids.length ? ids.length : i + 50);
        final vr = await _api('videos', {'part': 'snippet,statistics', 'id': chunk.join(',')}, key);
        vids.addAll(vr['items']);
      }
      vids.sort((a, b) => (b['snippet']['publishedAt'] as String).compareTo(a['snippet']['publishedAt']));

      setState(() {
        _chan = chan;
        _videos = vids;
        _error = null;
        _updated = DateTime.now();
      });
    } catch (e) {
      setState(() => _error = '$e');
    }
  }

  int _n(dynamic v) => int.tryParse('${v ?? 0}') ?? 0;

  @override
  Widget build(BuildContext context) {
    final s = _chan?['statistics'];
    return Padding(
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(children: [
            Text('Stats en direct', style: Theme.of(context).textTheme.headlineSmall),
            const Spacer(),
            if (_updated != null)
              Text('MAJ ${_updated!.hour.toString().padLeft(2, '0')}:${_updated!.minute.toString().padLeft(2, '0')}',
                  style: const TextStyle(color: Colors.white54)),
            IconButton(onPressed: _refresh, icon: const Icon(Icons.refresh)),
          ]),
          const SizedBox(height: 12),
          if (_error != null)
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(color: Colors.red.withValues(alpha: 0.15), borderRadius: BorderRadius.circular(10)),
              child: Text('Erreur : $_error', style: const TextStyle(color: Colors.redAccent)),
            ),
          if (s != null) ...[
            Row(children: [
              _card('Abonnes', _n(s['subscriberCount'])),
              const SizedBox(width: 12),
              // somme des vues actuelles des videos (plus a jour que le total de chaine)
              _card('Vues totales', _videos.fold<int>(0, (a, v) => a + _n(v['statistics']['viewCount']))),
              const SizedBox(width: 12),
              _card('Videos', _videos.length),
            ]),
            const SizedBox(height: 16),
            Expanded(
              child: ListView.separated(
                itemCount: _videos.length,
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (_, i) {
                  final v = _videos[i];
                  final st = v['statistics'];
                  final sn = v['snippet'];
                  final thumb = (sn['thumbnails']['medium'] ?? sn['thumbnails']['default'])['url'];
                  return ListTile(
                    leading: ClipRRect(
                        borderRadius: BorderRadius.circular(6),
                        child: Image.network(thumb, width: 90, fit: BoxFit.cover)),
                    title: Text(sn['title'], maxLines: 1, overflow: TextOverflow.ellipsis),
                    subtitle: Text('👍 ${_n(st['likeCount'])}   💬 ${_n(st['commentCount'])}'),
                    trailing: Text('${_n(st['viewCount'])}',
                        style: const TextStyle(color: Color(0xFFFFD400), fontWeight: FontWeight.bold, fontSize: 18)),
                    onTap: () => Process.run('cmd', ['/c', 'start', '', 'https://youtube.com/watch?v=${v['id']}']),
                  );
                },
              ),
            ),
          ] else if (_error == null)
            const Expanded(child: Center(child: CircularProgressIndicator())),
        ],
      ),
    );
  }

  Widget _card(String label, int value) => Expanded(
        child: Container(
          padding: const EdgeInsets.all(16),
          decoration: BoxDecoration(color: const Color(0xFF1C1C1C), borderRadius: BorderRadius.circular(14)),
          child: Column(children: [
            Text('$value', style: const TextStyle(fontSize: 26, fontWeight: FontWeight.bold)),
            const SizedBox(height: 4),
            Text(label, style: const TextStyle(color: Colors.white60, fontSize: 12)),
          ]),
        ),
      );
}

// ------------------------------------------------------------------ Reglages
class SettingsDialog extends StatefulWidget {
  const SettingsDialog({super.key});
  @override
  State<SettingsDialog> createState() => _SettingsDialogState();
}

class _SettingsDialogState extends State<SettingsDialog> {
  late final _bot = TextEditingController(text: Cfg.botDir);
  late final _py = TextEditingController(text: Cfg.python);
  late final _ff = TextEditingController(text: Cfg.ffmpegDir);
  late final _es = TextEditingController(text: Cfg.espeakDir);

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('Reglages'),
      content: SizedBox(
        width: 560,
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          TextField(controller: _bot, decoration: const InputDecoration(labelText: 'Dossier du projet Python')),
          const SizedBox(height: 10),
          TextField(controller: _py, decoration: const InputDecoration(labelText: 'Commande Python (python / py / chemin)')),
          const SizedBox(height: 10),
          TextField(controller: _ff, decoration: const InputDecoration(labelText: 'Dossier bin de FFmpeg')),
          const SizedBox(height: 10),
          TextField(controller: _es, decoration: const InputDecoration(labelText: 'Dossier eSpeak NG (pour Kokoro)')),
        ]),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('Annuler')),
        FilledButton(
          onPressed: () async {
            Cfg.botDir = _bot.text.trim();
            Cfg.python = _py.text.trim();
            Cfg.ffmpegDir = _ff.text.trim();
            Cfg.espeakDir = _es.text.trim();
            await Cfg.save();
            if (context.mounted) Navigator.pop(context);
          },
          child: const Text('Enregistrer'),
        ),
      ],
    );
  }
}
