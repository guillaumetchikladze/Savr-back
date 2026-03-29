from django.db import migrations, models


def seed_dietary_matches(apps, schema_editor):
    DietaryIngredientMatch = apps.get_model('recipes', 'DietaryIngredientMatch')
    rows = [
        # Viande rouge
        ('Viande rouge', 'bœuf'),
        ('Viande rouge', 'boeuf'),
        ('Viande rouge', 'veau'),
        ('Viande rouge', 'steak'),
        ('Viande rouge', 'entrecôte'),
        ('Viande rouge', 'rumsteck'),
        ('Viande rouge', 'haché'),
        ('Viande rouge', 'bœuf haché'),
        ('Viande rouge', 'boeuf haché'),
        ('Viande rouge', 'viande rouge'),
        # Porc
        ('Porc', 'porc'),
        ('Porc', 'lard'),
        ('Porc', 'bacon'),
        ('Porc', 'jambon'),
        ('Porc', 'saucisse'),
        ('Porc', 'chorizo'),
        ('Porc', 'pancetta'),
        ('Porc', 'poitrine'),
        # Agneau
        ('Agneau', 'agneau'),
        ('Agneau', 'mouton'),
        # Abats
        ('Abats', 'abats'),
        ('Abats', 'foie'),
        ('Abats', 'rognon'),
        ('Abats', 'andouille'),
        ('Abats', 'tripes'),
        ('Abats', 'ris'),
        # Poisson
        ('Poisson', 'poisson'),
        ('Poisson', 'saumon'),
        ('Poisson', 'cabillaud'),
        ('Poisson', 'colin'),
        ('Poisson', 'thon'),
        ('Poisson', 'maquereau'),
        ('Poisson', 'sardine'),
        ('Poisson', 'anchois'),
        ('Poisson', 'truite'),
        ('Poisson', 'dorade'),
        ('Poisson', 'bar'),
        ('Poisson', 'sole'),
        ('Poisson', 'lieu'),
        # Fruits de mer
        ('Fruits de mer', 'crevette'),
        ('Fruits de mer', 'crabe'),
        ('Fruits de mer', 'homard'),
        ('Fruits de mer', 'langouste'),
        ('Fruits de mer', 'langoustine'),
        ('Fruits de mer', 'moule'),
        ('Fruits de mer', 'huître'),
        ('Fruits de mer', 'huitre'),
        ('Fruits de mer', 'calamar'),
        ('Fruits de mer', 'poulpe'),
        ('Fruits de mer', 'encornet'),
        ('Fruits de mer', 'noix de saint-jacques'),
        # Très épicé
        ('Très épicé', 'piment'),
        ('Très épicé', 'espelette'),
        ('Très épicé', 'harissa'),
        ('Très épicé', 'cayenne'),
        ('Très épicé', 'chili'),
        ('Très épicé', 'wasabi'),
        ('Très épicé', 'tabasco'),
        ('Très épicé', 'sriracha'),
        # Champignons
        ('Champignons', 'champignon'),
        ('Champignons', 'shiitake'),
        ('Champignons', 'cèpe'),
        ('Champignons', 'girolle'),
        ('Champignons', 'pleurote'),
        # Coriandre
        ('Coriandre', 'coriandre'),
        # Chou / choucroute
        ('Chou / choucroute', 'chou'),
        ('Chou / choucroute', 'choucroute'),
        ('Chou / choucroute', 'kale'),
        ('Chou / choucroute', 'chou chinois'),
        # Tofu / soja
        ('Tofu / soja', 'tofu'),
        ('Tofu / soja', 'soja'),
        ('Tofu / soja', 'edamame'),
        ('Tofu / soja', 'tempeh'),
        ('Tofu / soja', 'sauce soja'),
        # Olives
        ('Olives', 'olive'),
        # Allergies — libellés exacts app
        ('Gluten', 'gluten'),
        ('Gluten', 'blé'),
        ('Gluten', 'seigle'),
        ('Gluten', 'orge'),
        ('Gluten', 'malt'),
        ('Gluten', 'couscous'),
        ('Gluten', 'semoule'),
        ('Gluten', 'panko'),
        ('Gluten', 'chapelure'),
        ('Gluten', 'épeautre'),
        ('Gluten', 'kamut'),
        ('Lactose / lait', 'lait'),
        ('Lactose / lait', 'crème'),
        ('Lactose / lait', 'beurre'),
        ('Lactose / lait', 'fromage'),
        ('Lactose / lait', 'yaourt'),
        ('Lactose / lait', 'yogurt'),
        ('Lactose / lait', 'lactose'),
        ('Lactose / lait', 'mozzarella'),
        ('Lactose / lait', 'parmesan'),
        ('Lactose / lait', 'emmental'),
        ('Lactose / lait', 'comté'),
        ('Lactose / lait', 'crème fraîche'),
        ('Lactose / lait', 'mascarpone'),
        ('Lactose / lait', 'ricotta'),
        ('Œufs', 'œuf'),
        ('Œufs', 'oeuf'),
        ('Œufs', 'blanc d\'oeuf'),
        ('Œufs', 'jaune d\'oeuf'),
        ('Arachides', 'arachide'),
        ('Arachides', 'cacahuète'),
        ('Arachides', 'cacahuete'),
        ('Arachides', 'peanut'),
        ('Fruits à coque', 'amande'),
        ('Fruits à coque', 'noisette'),
        ('Fruits à coque', 'noix'),
        ('Fruits à coque', 'pistache'),
        ('Fruits à coque', 'cajou'),
        ('Fruits à coque', 'macadamia'),
        ('Fruits à coque', 'noix de cajou'),
        ('Soja', 'soja'),
        ('Soja', 'tofu'),
        ('Soja', 'edamame'),
        ('Soja', 'tempeh'),
        ('Soja', 'sauce soja'),
        ('Crustacés', 'crevette'),
        ('Crustacés', 'crabe'),
        ('Crustacés', 'homard'),
        ('Crustacés', 'langouste'),
        ('Crustacés', 'langoustine'),
        ('Mollusques', 'moule'),
        ('Mollusques', 'huître'),
        ('Mollusques', 'huitre'),
        ('Mollusques', 'calamar'),
        ('Mollusques', 'poulpe'),
        ('Mollusques', 'escargot'),
        ('Mollusques', 'seiche'),
        ('Poisson', 'poisson'),
        ('Poisson', 'saumon'),
        ('Poisson', 'thon'),
        ('Poisson', 'cabillaud'),
        ('Poisson', 'anchois'),
        ('Sésame', 'sésame'),
        ('Sésame', 'sesame'),
        ('Sésame', 'tahini'),
        ('Moutarde', 'moutarde'),
        ('Sulfites', 'sulfite'),
    ]
    for label, kw in rows:
        DietaryIngredientMatch.objects.get_or_create(
            preference_label=label,
            match_keyword=kw,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0056_mealplan_slot_key_custom_slot'),
    ]

    operations = [
        migrations.CreateModel(
            name='DietaryIngredientMatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('preference_label', models.CharField(db_index=True, max_length=120)),
                ('match_keyword', models.CharField(max_length=200)),
            ],
            options={
                'verbose_name': 'Correspondance préférence → ingrédient',
                'verbose_name_plural': 'Correspondances préférences → ingrédients',
                'unique_together': {('preference_label', 'match_keyword')},
            },
        ),
        migrations.RunPython(seed_dietary_matches, noop_reverse),
    ]
