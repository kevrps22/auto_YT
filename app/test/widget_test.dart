import 'package:flutter_test/flutter_test.dart';
import 'package:yt_studio/main.dart';

void main() {
  testWidgets('App builds', (tester) async {
    await tester.pumpWidget(const YtStudioApp());
    expect(find.text('Generer'), findsWidgets);
  });
}
