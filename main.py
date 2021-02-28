import json
from datetime import date, timedelta
from decimal import *
from math import ceil
from typing import List

import isodate
import pdfkit
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
from slugify import slugify

templates_env = Environment(
    loader=FileSystemLoader('templates'),
    autoescape=select_autoescape(['html']),
)


def format_invoice_num(n: int) -> str:
    s = '{:0>6}'.format(n)
    return f'{s[0:3]} {s[3:6]}'


def increment_invoice_num() -> str:
    with open('next_invoice_num.json') as f:
        current = json.load(f)
    with open('next_invoice_num.json', 'w') as f:
        json.dump(current + 1, f)
        return format_invoice_num(current)


def get_currency_symbol(currency_code: str) -> str:
    if currency_code == 'gbp':
        return '£'
    elif currency_code == 'usd':
        return '$'
    else:
        raise


def format_date(d: date) -> str:
    return d.strftime('%b %-d, %Y')


def md_to_html(line: str) -> str:
    if line.startswith('**') and line.endswith('**'):
        return f'<strong>{line[2:-2]}</strong>'
    elif line.startswith('_') and line.endswith('_'):
        return f'<em>{line[1:-1]}</em>'
    else:
        return line


def format_money(d: Decimal, currency_code: str = 'usd') -> str:
    return get_currency_symbol(currency_code) + '{:,.2f}'.format(d)


templates_env.filters['cursym'] = get_currency_symbol
templates_env.filters['date'] = format_date
templates_env.filters['md'] = md_to_html
templates_env.filters['money'] = format_money

with open('profile.json') as profile_file:
    profile = json.load(profile_file)


def clockify_get(url: str, params=None, **kwargs):
    workspace_id = profile['clockify']['workspace_id']
    url = f'https://api.clockify.me/api/v1/workspaces/{workspace_id}/{url}'
    kwargs.update({'headers': {'x-api-key': profile['clockify']['api_key']}})
    return requests.get(url, params=params, **kwargs).json()


def clockify_user_get(url: str, params=None, **kwargs):
    user_id = profile['clockify']['user_id']
    return clockify_get(f'user/{user_id}/{url}', params=params, **kwargs)


def wise_get(url: str, params=None, **kwargs):
    kwargs.update({'headers': {'authorization': 'bearer ' + profile['wise']['api_key']}})
    return requests.get('https://api.transferwise.com/v1/' + url, params=params, **kwargs).json()


def get_exchange_rate_from_usd(to: str) -> Decimal:
    r = wise_get('rates', {'source': 'usd', 'target': to})
    return Decimal(str(r[0]['rate']))


class WorkItem:
    rounded_hours = None
    total = None

    def __init__(self, project: str, description: str, rate: Decimal, hours: float):
        self.project = project
        self.description = description
        self.rate = rate
        self.hours = hours

    def get_rounded_hours(self, time_step: int) -> Decimal:
        """
        Gets this work item's hours rounded up to the nearest given time step.

        :param time_step: The increment in minutes
        :return: The number of hours
        """
        minutes = self.hours * 60
        return Decimal(ceil(minutes / time_step) * time_step) / 60

    def get_total(self, time_step: int) -> Decimal:
        """
        Gets the amount of money to bill given this work item's hours,
        rounded up to the nearest given time step, and the rate.

        :param time_step: The increment in minutes
        :return: The amount of money to bill in the same currency as the rate
        """
        return self.rate * self.get_rounded_hours(time_step)

    def set_rounded_hours(self, time_step: int):
        self.rounded_hours = self.get_rounded_hours(time_step)

    def set_total(self, time_step: int):
        self.total = self.get_total(time_step)


class Client:

    def __init__(self, name: str, work_items: List[WorkItem]):
        self.name = name
        self.work_items = work_items
        self.profile = profile['clients'][self.name]
        self.invoice_num = increment_invoice_num()
        self.address = self.profile['address']
        self.bill_time_step = int(self.profile['bill_time_step'])
        self.currency_code = self.profile['currency_code']
        self.days_until_due = int(self.profile['days_until_due'])
        for item in work_items:
            item.set_rounded_hours(self.bill_time_step)
            item.set_total(self.bill_time_step)

    def get_time_until_due(self) -> timedelta:
        return timedelta(days=self.days_until_due)

    def get_total_due(self) -> Decimal:
        return sum(i.get_total(self.bill_time_step) for i in self.work_items)

    def generate_invoice(self):
        template = templates_env.get_template('invoice.html')

        total_due = self.get_total_due()
        exchange_rate = 1
        if self.currency_code != 'usd':
            exchange_rate = get_exchange_rate_from_usd(self.currency_code)

        invoice_date = date.today()
        invoice_due = invoice_date + self.get_time_until_due()
        invoice_data = {
            'invoice_date': invoice_date,
            'invoice_due': invoice_due,
            'client': self,
            'work_items': self.work_items,
            'work_total': total_due,
            'work_total_converted': total_due * exchange_rate,
            'bank_account': profile['bank_accounts'][self.currency_code],
            'exchange_rate': exchange_rate,
        }
        invoice_data.update(profile)
        return template.render(invoice_data)


def merge_time_entries(project, time_entries) -> WorkItem:
    project_name = project['name']
    description = time_entries[0]['description']
    rate = Decimal(project['hourlyRate']['amount']) / 100
    delta = timedelta()
    for entry in time_entries:
        delta += isodate.parse_duration(entry['timeInterval']['duration'])
    hours = delta.total_seconds() / 60 / 60
    return WorkItem(project_name, description, rate, hours)


def get_work_items(projects, time_entries, client_name: str):
    project_ids = set(p['id'] for p in projects if p['clientName'] == client_name)
    time_entries = [e for e in time_entries if e['projectId'] in project_ids]
    for description in sorted(set(e['description'] for e in time_entries)):
        group = [e for e in time_entries if e['description'] == description]
        project = next(p for p in projects if p['id'] == group[0]['projectId'])
        yield merge_time_entries(project, group)


def get_clients():
    uninvoiced_tag_id = profile['clockify']['uninvoiced_tag_id']
    time_entries = clockify_user_get('time-entries', {'tags': uninvoiced_tag_id})
    project_ids = set(t['projectId'] for t in time_entries)
    projects = [p for p in clockify_get('projects') if p['id'] in project_ids]
    for client_name in set(p['clientName'] for p in projects):
        yield Client(client_name, list(get_work_items(projects, time_entries, client_name)))


def main():
    for client in get_clients():
        filename = slugify(f'{client.name}_{client.invoice_num}', separator='_')
        pdfkit.from_string(client.generate_invoice(), f'out/{filename}.pdf',
                           css='assets/styles.css')


if __name__ == '__main__':
    main()
