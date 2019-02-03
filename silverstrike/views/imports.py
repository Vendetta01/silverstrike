import csv
from datetime import date

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views import generic

from silverstrike import forms
from silverstrike import importers
from silverstrike import models


class ImportView(LoginRequiredMixin, generic.TemplateView):
    template_name = 'silverstrike/import.html'


class ImportUploadView(LoginRequiredMixin, generic.edit.CreateView):
    model = models.ImportFile
    form_class = forms.ImportUploadForm
    template_name = 'silverstrike/import_upload.html'

    def form_valid(self, form):
        self.object = form.save()
        account = form.cleaned_data['account']
        importer = form.cleaned_data['importer']
        return HttpResponseRedirect(
            reverse('import_process', args=[self.object.pk, account.pk, importer]))


class ImportProcessView(LoginRequiredMixin, generic.TemplateView):
    template_name = 'silverstrike/import_configure_upload.html'

    def get_context_data(self, **kwargs):
        context = super(ImportProcessView, self).get_context_data(**kwargs)
        file = models.ImportFile.objects.get(uuid=self.kwargs['uuid'])
        importer = self.kwargs['importer']

        iban_accounts = { a.iban: a for a in models.Account.objects.exclude(iban='')}
        context['data'] = importers.IMPORTERS[importer].import_transactions(file.file.path)
        max_date = date(1970, 1, 1)
        min_date = date(3000, 1, 1)
        for datum in context['data']:
            if datum.book_date < min_date:
                min_date = datum.book_date
            if datum.book_date > max_date:
                max_date = datum.book_date
            if datum.iban and datum.iban in iban_accounts:
                datum.account = iban_accounts[datum.iban]

        # duplicate detection
        transactions = set()
        for t in models.Transaction.objects.date_range(min_date, max_date):
            if t.is_transfer:
                transactions.add('{}-{}-{}'.format(t.src_id, t.date, t.amount))
                transactions.add('{}-{}-{}'.format(t.dst_id, t.date, t.amount))
            elif t.is_deposit:
                transactions.add('{}-{}-{}'.format(t.src_id, t.date, t.amount))
            elif t.is_withdraw:
                transactions.add('{}-{}-{}'.format(t.dst_id, t.date, t.amount))
        for datum in context['data']:
            if hasattr(datum.account, 'id') and '{}-{}-{}'.format(datum.account.id, datum.book_date, abs(datum.amount)) in transactions:
                datum.ignore = True

        context['recurrences'] = models.RecurringTransaction.objects.exclude(
            interval=models.RecurringTransaction.DISABLED).order_by('title')
        return context

    def post(self, request, *args, **kwargs):
        file = models.ImportFile.objects.get(uuid=self.kwargs['uuid'])
        importer = self.kwargs['importer']
        data = importers.IMPORTERS[importer].import_transactions(file.file.path)
        for i in range(len(data)):
            title = request.POST.get('title-{}'.format(i), '')
            account = request.POST.get('account-{}'.format(i), '')
            recurrence = int(request.POST.get('recurrence-{}'.format(i), '-1'))
            ignore = request.POST.get('ignore-{}'.format(i), '')
            book_date = data[i].book_date
            date = data[i].transaction_date
            if not (title and account) or ignore:
                continue
            amount = float(data[i].amount)
            if amount == 0:
                continue
            account, _ = models.Account.objects.get_or_create(
                name=account,
                defaults={'account_type': models.Account.FOREIGN})
            if not account.iban and hasattr(data[i], 'iban'):
                account.iban = data[i].iban
                account.save()
            transaction = models.Transaction()
            if account.account_type == models.Account.PERSONAL:
                transaction.transaction_type = models.Transaction.TRANSFER
                if amount < 0:
                    transaction.src_id = self.kwargs['account']
                    transaction.dst = account
                else:
                    transaction.src = account
                    transaction.dst_id = self.kwargs['account']
            elif account.account_type == models.Account.FOREIGN:
                if amount < 0:
                    transaction.transaction_type = models.Transaction.WITHDRAW
                    transaction.src_id = self.kwargs['account']
                    transaction.dst = account
                else:
                    transaction.transaction_type = models.Transaction.DEPOSIT
                    transaction.dst_id = self.kwargs['account']
                    transaction.src = account
            transaction.title = title
            transaction.date = date
            transaction.amount = abs(amount)

            if recurrence > 0:
                transaction.recurrence_id = recurrence
            transaction.save()

            models.Split.objects.create(
                title=title,
                amount=amount,
                date=book_date,
                transaction=transaction,
                account_id=self.kwargs['account'],
                opposing_account=account
                )
            models.Split.objects.create(
                title=title,
                amount=-amount,
                date=date,
                transaction=transaction,
                account=account,
                opposing_account_id=self.kwargs['account']
                )
        return HttpResponseRedirect('/')


class ImportFireflyView(LoginRequiredMixin, generic.edit.CreateView):
    model = models.ImportFile
    fields = ['file']
    template_name = 'silverstrike/import_upload.html'

    def form_valid(self, form):
        self.object = form.save()
        importers.firefly.import_firefly(self.object.file.path)
        return HttpResponseRedirect(reverse('index'))


class ExportView(LoginRequiredMixin, generic.edit.FormView):
    template_name = 'silverstrike/export.html'
    form_class = forms.ExportForm

    def form_valid(self, form):
        response = HttpResponse(content_type='text/csv')

        splits = models.Split.objects.date_range(
            form.cleaned_data['start'], form.cleaned_data['end']).transfers_once()
        splits = splits.filter(account__in=form.cleaned_data['accounts'])
        csv_writer = csv.writer(response, delimiter=';')
        headers = [
            'account',
            'opposing_account',
            'date',
            'amount',
            'category'
            ]
        csv_writer.writerow(headers)
        for split in splits.values_list('account__name', 'opposing_account__name',
                                        'date', 'amount', 'category__name'):
            csv_writer.writerow(split)

        response['Content-Disposition'] = 'attachment; filename=export.csv'
        return response
