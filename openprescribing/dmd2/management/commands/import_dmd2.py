import csv
import glob
import os
from lxml import etree

from openpyxl import load_workbook

from django.core.management import BaseCommand
from django.db import connection, transaction
from django.db.models import fields as django_fields

from dmd2 import models
from dmd2.models import AMP, AMPP, VMP, VMPP
from frontend.models import Presentation


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('dmd_data_path')
        parser.add_argument('mapping_path')
        parser.add_argument('logs_path')

    def handle(self, *args, **kwargs):
        self.dmd_data_path = kwargs['dmd_data_path']
        self.mapping_path = kwargs['mapping_path']
        self.logs_path = kwargs['logs_path']

        self.log_keys = [
            'dmd-objs-present-in-mapping-only',
            'vmps-with-inferred-bnf-code',
            'vmps-with-no-bnf-code',
            'bnf-codes-with-multiple-dmd-objs',
            'bnf-codes-with-multiple-dmd-objs-and-no-inferred-name',
            'vmpps-with-different-bnf-code-to-vmp',
            'ampps-with-different-bnf-code-to-amp',
        ]
        self.logs = {key: list() for key in self.log_keys}

        with transaction.atomic():
            self.import_dmd()
            self.import_bnf_code_mapping()
            self.set_vmp_bnf_codes()
            self.set_dmd_names()

        self.log_other_oddities()
        self.write_logs()

    def import_dmd(self):
        # dm+d data is provided in several XML files:
        #
        # * f_amp2_3[ddmmyy].xml
        # * f_ampp2_3[ddmmyy].xml
        # * f_gtin2_0[ddmmyy].xml
        # * f_ingredient2_3[ddmmyy].xml
        # * f_lookup2_3[ddmmyy].xml
        # * f_vmp2_3[ddmmyy].xml
        # * f_vmpp2_3[ddmmyy].xml
        # * f_vtm2_3[ddmmyy].xml
        #
        # Each file contains a list or lists of elements that correspond to
        # instances of one of the models in models.py.
        #
        # Each such element has the structure:
        #
        # <OBJ_TYPE>
        #   <FIELD1>value</FIELD1>
        #   <FIELD2>value</FIELD2>
        #   <FIELD3>value</FIELD3>
        # </OBJ_TYPE>
        #
        # These elements are arranged differently in different files.
        #
        # The ingredient and VTM files just contain a list of elements
        # corresponding to instances of Ing and VTM respectively.  For
        # instance:
        #
        # <INGREDIENT_SUBSTANCES>
        #     <!-- Generated by NHSBSA PPD -->
        #     <ING>...</ING>
        #     <ING>...</ING>
        #     ...
        # </INGREDIENT_SUBSTANCES>
        #
        # The VMP, VMPP, AMP, AMPP and lookup files contain several lists of
        # elements, corresponding to multiple types of objects.  For instance:
        #
        # <VIRTUAL_MED_PRODUCTS>
        #     <!-- Generated by NHSBSA PPD -->
        #     <VMPS>
        #         <VMP>...</VMP>
        #         <VMP>...</VMP>
        #         ...
        #     </VMPS>
        #     <VIRTUAL_PRODUCT_INGREDIENT>
        #         <VPI>...</VPI>
        #         <VPI>...</VPI>
        #         ...
        #     </VIRTUAL_PRODUCT_INGREDIENT>
        #     <ONT_DRUG_FORM>
        #         <ONT>...</ONT>
        #         <ONT>...</ONT>
        #         ...
        #     </ONT_DRUG_FORM>
        #     ...
        # <VIRTUAL_MED_PRODUCTS>
        #
        # The GTIN file is a bit weird and the data requires a little massaging
        # before it can be imported.  See code below.
        #
        # Since the data model contains foreign key constraints, the order we
        # import the files is significant.
        #
        # When importing the data, we first delete all existing instances,
        # because the IDs of some SNOMED objects can change.

        # lookup
        for elements in self.load_elements('lookup'):
            model_name = self.make_model_name(elements.tag)
            model = getattr(models, model_name)
            self.import_model(model, elements)

        # ingredient
        elements = self.load_elements('ingredient')
        self.import_model(models.Ing, elements)

        # vtm
        elements = self.load_elements('vtm')
        self.import_model(models.VTM, elements)

        # vmp
        for elements in self.load_elements('vmp'):
            model_name = self.make_model_name(elements[0].tag)
            model = getattr(models, model_name)
            self.import_model(model, elements)

        # vmpp
        for elements in self.load_elements('vmpp'):
            if elements[0].tag == 'CCONTENT':
                # TODO Handle CCONTENT
                continue

            model_name = self.make_model_name(elements[0].tag)
            model = getattr(models, model_name)
            self.import_model(model, elements)

        # amp
        for elements in self.load_elements('amp'):
            if len(elements) == 0:
                # For test data, some lists of elements are empty (eg
                # AP_INFORMATION), and so we can't look at the first element of
                # the list to get the name of the corresponding model.
                continue

            model_name = self.make_model_name(elements[0].tag)
            model = getattr(models, model_name)
            self.import_model(model, elements)

        # ampp
        for elements in self.load_elements('ampp'):
            if len(elements) == 0:
                # For test data, some lists of elements are empty (eg
                # APPLIANCE_PACK_INFO), and so we can't look at the first
                # element of the list to get the name of the corresponding
                # model.
                continue

            if elements[0].tag == 'CCONTENT':
                # TODO Handle CCONTENT
                continue

            model_name = self.make_model_name(elements[0].tag)
            model = getattr(models, model_name)
            self.import_model(model, elements)

        # gtin
        elements = self.load_elements('gtin')[0]
        for element in elements:
            assert element[0].tag == 'AMPPID'
            assert element[1].tag == 'GTINDATA'

            element[0].tag = 'APPID'
            for gtinelt in element[1]:
                element.append(gtinelt)
            element.remove(element[1])
        self.import_model(models.GTIN, elements)

    def load_elements(self, filename_fragment):
        '''Return list of non-comment top-level elements in given file.'''

        paths = glob.glob(
            os.path.join(self.dmd_data_path, 'f_{}2_*.xml'.format(filename_fragment))
        )
        assert len(paths) == 1

        with open(paths[0]) as f:
            doc = etree.parse(f)

        root = doc.getroot()
        elements = list(root)
        assert isinstance(elements[0], etree._Comment)
        return elements[1:]

    def import_model(self, model, elements):
        '''Import model instances from list of XML elements.'''

        model.objects.all().delete()

        boolean_field_names = [
            f.name
            for f in model._meta.fields
            if isinstance(f, django_fields.BooleanField)
        ]

        table_name = model._meta.db_table
        column_names = [
            f.db_column or f.name
            for f in model._meta.fields
            if not isinstance(f, django_fields.AutoField)
        ]
        sql = 'INSERT INTO {} ({}) VALUES ({})'.format(
            table_name, ', '.join(column_names), ', '.join(['%s'] * len(column_names))
        )

        values = []

        for element in elements:
            row = {}

            for field_element in element:
                name = field_element.tag.lower()
                if name == 'desc':
                    # "desc" is a really unhelpful field name if you're writing
                    # SQL!
                    name = 'descr'
                elif name == 'dnd':
                    # For consistency with the rest of the data, we rename
                    # "dnd" to "dndcd", as it is a foreign key field.
                    name = 'dndcd'

                value = field_element.text
                row[name] = value

            for name in boolean_field_names:
                row[name] = name in row

            values.append([row.get(name) for name in column_names])

        with connection.cursor() as cursor:
            cursor.executemany(sql, values)

    def make_model_name(self, tag_name):
        '''Construct name of Django model from XML tag name.'''

        if tag_name in ['VTM', 'VPI', 'VMP', 'VMPP', 'AMP', 'AMPP', 'GTIN']:
            return tag_name
        else:
            return ''.join(tok.title() for tok in tag_name.split('_'))

    def import_bnf_code_mapping(self):
        type_to_model = {
            ('Presentation', 'VMP'): VMP,
            ('Presentation', 'AMP'): AMP,
            ('Pack', 'VMP'): VMPP,
            ('Pack', 'AMP'): AMPP,
        }

        wb = load_workbook(filename=self.mapping_path)
        rows = wb.active.rows

        headers = next(rows)
        assert headers[0].value == 'Presentation / Pack Level'
        assert headers[1].value == 'VMP / AMP'
        assert headers[2].value == 'BNF Code'
        assert headers[4].value == 'SNOMED Code'

        VMP.objects.update(bnf_code=None)
        AMP.objects.update(bnf_code=None)
        VMPP.objects.update(bnf_code=None)
        AMPP.objects.update(bnf_code=None)

        for ix, row in enumerate(rows):
            model = type_to_model[(row[0].value, row[1].value)]

            bnf_code = row[2].value
            snomed_id = row[4].value

            if bnf_code is None or snomed_id is None:
                continue

            if bnf_code == "'" or snomed_id == "'":
                continue

            bnf_code = bnf_code.lstrip("'")
            snomed_id = snomed_id.lstrip("'")

            try:
                obj = model.objects.get(id=snomed_id)
            except model.DoesNotExist:
                key = (model.__name__, snomed_id)
                self.logs['dmd-objs-present-in-mapping-only'].append(key)
                continue

            obj.bnf_code = bnf_code
            obj.save()

    def set_vmp_bnf_codes(self):
        '''There are many VMPs that do not have BNF codes set in the mapping,
        but whose VMPPs all have the same BNF code.  In these cases, we think
        that the VMPPs' BNF code can be applied to the VMP too.
        '''

        vmps = VMP.objects.filter(bnf_code__isnull=True).prefetch_related('vmpp_set')

        for vmp in vmps:
            vmpp_bnf_codes = {
                vmpp.bnf_code for vmpp in vmp.vmpp_set.all() if vmpp.bnf_code
            }

            if len(vmpp_bnf_codes) == 1:
                self.logs['vmps-with-inferred-bnf-code'].append([vmp.id])
                vmp.bnf_code = list(vmpp_bnf_codes)[0]
                vmp.save()
            else:
                self.logs['vmps-with-no-bnf-code'].append([vmp.id])

    def set_dmd_names(self):
        '''Sets Presentation.dmd_name by finding linked dm+d objects with the
        same BNF code.  We look for matching VMPs first, then AMPs, then VMPPs,
        then AMPPs.  We look at VMPs and AMPs first, since BNF codes are
        supposed to correspond to products.  We prefer VMPs to AMPs (and VMPPs
        to AMPPs) since a VMP's generic AMPs usually share the VMP's BNF code
        (and also for VMPPs and AMPPs).

        In many cases there will be multiple linked objects of the same type,
        with the same BNF code.  For instance:

        * There may be several VMPs with the same BNF code.  This often happens
          where each size/flavour/colour of a presentation has its own VMP, eg:
          * Coal tar 10% in Yellow soft paraffin
          * Coal tar 10% in White soft paraffin
          * etc
        * There may be several AMPPs with the same BNF code, but with a
          different BNF code to any other member of the VMPs family.  There
          doesn't seem to be much of a pattern for why this happens.

        When there are multiple linked objects of the same type, we try to
        infer a name for the presentation by looking at the common left
        substring of all the linked objects' names.
        '''

        # First, we clear all existing dmd_names.
        Presentation.objects.update(dmd_name=None)

        # We build a mapping from BNF code to another mapping, which maps from
        # classes to a set of names of objects of that class with that BNF
        # code.
        #
        # eg:
        #   bnf_code_to_cls_to_names = {
        #     '1003020U0BBADAI': {
        #       VMP: set(),
        #       VMPP: set(),
        #       AMP: {
        #           u'Voltarol 2.32% gel'
        #       },
        #       AMPP: {
        #           u'Voltarol 2.32% gel (GSK) 100 gram',
        #           u'Voltarol 2.32% gel (GSK) 30 gram',
        #           u'Voltarol 2.32% gel (GSK) 50 gram',
        #       },
        #   }
        #   '1003020U0AAAIAI': {...},
        #   ...
        #   }

        bnf_code_to_cls_to_names = {}

        for cls in VMP, AMP, VMPP, AMPP:
            for obj in cls.objects.all():
                if obj.bnf_code is None:
                    continue
                if obj.bnf_code not in bnf_code_to_cls_to_names:
                    bnf_code_to_cls_to_names[obj.bnf_code] = {
                        VMP: set(),
                        AMP: set(),
                        VMPP: set(),
                        AMPP: set(),
                    }
                cls_to_names = bnf_code_to_cls_to_names[obj.bnf_code]
                cls_to_names[type(obj)].add(obj.nm)

        # For each presentation, we find all VMPs will that presentation's BNF
        # code.  If there is one VMP, we take its name and assign that to the
        # presentation's dmd_name field.  If there are multiple VMPs, we try to
        # infer a name using get_common_name().  If no such VMPs exist, we look
        # at AMPs, then VMPPs, then AMPPs.

        for presentation in Presentation.objects.all():
            try:
                cls_to_names = bnf_code_to_cls_to_names[presentation.bnf_code]
            except KeyError:
                continue

            for cls in VMP, AMP, VMPP, AMPP:
                names = cls_to_names[cls]
                if len(names) == 1:
                    presentation.dmd_name = list(names)[0]
                    presentation.save()
                    break
                elif len(names) > 1:
                    self.logs['bnf-codes-with-multiple-dmd-objs'].append(
                        [presentation.bnf_code]
                    )
                    common_name = get_common_name(list(names))
                    if common_name is None:
                        self.logs[
                            'bnf-codes-with-multiple-dmd-objs-and-no-inferred-name'
                        ].append([presentation.bnf_code])
                    else:
                        presentation.dmd_name = common_name
                        presentation.save()
                    break

    def log_other_oddities(self):
        '''Log oddities in the data that are not captured elsewhere.'''

        sql = '''
        SELECT vmpp.vppid
        FROM dmd2_vmp vmp
        INNER JOIN dmd2_vmpp vmpp
            ON vmp.vpid = vmpp.vpid
        WHERE vmp.bnf_code IS NOT NULL
          AND vmp.bnf_code != vmpp.bnf_code
        '''

        with connection.cursor() as cursor:
            cursor.execute(sql)
            self.logs['vmpps-with-different-bnf-code-to-vmp'] = cursor.fetchall()

        sql = sql.replace('v', 'a')

        with connection.cursor() as cursor:
            cursor.execute(sql)
            self.logs['ampps-with-different-bnf-code-to-amp'] = cursor.fetchall()

    def write_logs(self):
        '''Record summary and details of oddities we've found in the data.

        We log (summary and details) the following things:
         * dm+d objects only present in mapping
         * VMPs with inferred BNF codes
         * VMPs without BNF codes
         * BNF codes with multiple dm+d objects
         * BNF codes with multiple dm+d objects where a name cannot be
           inferred
         * VMPPs that have different BNF code to their VMP
         * AMPPs that have different BNF code to their AMP

        We also log summaries of the number of objects imported.
        '''

        os.mkdir(self.logs_path)

        for key in self.log_keys:
            with open(os.path.join(self.logs_path, key + '.csv'), 'w') as f:
                writer = csv.writer(f)
                writer.writerows(self.logs[key])

        with open(os.path.join(self.logs_path, 'summary.csv'), 'w') as f:
            writer = csv.writer(f)
            for model in [VMP, AMP, VMPP, AMPP]:
                writer.writerow([model.__name__, model.objects.count()])
            for key in self.log_keys:
                writer.writerow([key, len(self.logs[key])])


def get_common_name(names):
    '''Find left substring common to all names, by splitting names on spaces,
    and possibly ignoring certain common words.

    >>> get_common_name([
        'Polyfield Soft Vinyl Patient Pack with small gloves',
        'Polyfield Soft Vinyl Patient Pack with medium gloves',
        'Polyfield Soft Vinyl Patient Pack with large gloves',
    ])
    'Polyfield Soft Vinyl Patient Pack'

    Returns None if left substring is less than half the length (in words) of
    the first name.

    This function could be improved.  For instance, in dm+d there are several
    cases where names of two products with the same BNF code differ by one word
    in the middle:

    * Imodium 2mg capsules
    * Imodium Original 2mg capsules

    And there are several cases where names of two products with the same BNF
    code have different versions of the same ratio:

    * Taurolidine 5g/250ml intraperitoneal solution bottles
    * Taurolidine 2g/100ml intraperitoneal solution bottles
    '''

    common_name = names[0]

    for name in names[1:]:
        words = []
        for word1, word2 in zip(common_name.split(), name.split()):
            if word1 != word2:
                break
            words.append(word1)

        if not words:
            return None

        if words[-1] in ['with', 'in', 'size']:
            # If the common substring ends in one of these words, we want to
            # ignore it.
            #
            # For example:
            #
            # * with:
            #   * Celluloid colostomy cup with solid rim, small
            #   * Celluloid colostomy cup with sponge rim, small
            # * in:
            #   * Coal tar 10% in Yellow soft paraffin
            #   * Coal tar 10% in White soft paraffin
            # * size:
            #   * Genesis 3H constrictor ring size 3
            #   * Genesis 3H constrictor ring size 4
            words = words[:-1]

        if words[-1] == 'oral':
            # If the common substring ends with 'oral', it probably means that
            # there are two names corresponding to products with the same BNF
            # code where one is a solution and the other is a suspension.
            #
            # For example:
            #
            #  * Acetazolamide 350mg/5ml oral solution
            #  * Acetazolamide 350mg/5ml oral suspension
            words = words[:-1]

        common_name = ' '.join(words)

    if len(common_name.split()) < len(names[0].split()) / 2:
        return None

    return common_name.strip()
